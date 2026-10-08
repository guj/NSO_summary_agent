import asyncio
import time
from types import SimpleNamespace
import pytest

from agent.llm_budget import ProviderBudgetExceeded
from diagnostic_mas.case import Budget, CaseFile, add_evidence, add_diagnosis
from diagnostic_mas.dataplane_scheduler import run_concurrent, Reservations, ReservedClient, llm_request
from diagnostic_mas.investigation import Investigation


def fixture():
    case = CaseFile(budget=Budget(0, 0))
    add_evidence(case, {'kind': 'spine', 'role': 'service'})
    return case


def candidates(*devices):
    ev = {'payload': {}}
    return [(ev, {'name': str(i), 'service_type': 'l2sts', 'devices': d}) for i, d in enumerate(devices)]


async def run(case, selected, verify, workers=2):
    return await run_concurrent(selected, workers=workers, client=SimpleNamespace(), settings=None,
        case=case, device_names=set(), tools_cap=40, openai_client=None, verify_one=verify, log=lambda s: None)


@pytest.mark.asyncio
async def test_disjoint_overlap_queue_and_isolated_evidence():
    case = fixture()
    selected = candidates(['a','b'], ['b','c'], ['d','e'])
    active = set()
    order = []
    peak = 0
    async def verify(client, settings, child, *, record, session, **kw):
        nonlocal peak
        assert child is not case
        name = record['name']
        assert not (set(record['devices']) & {d for n in active for d in selected[int(n)][1]['devices']})
        active.add(name); order.append(name); peak = max(peak,len(active))
        eid = add_evidence(child, {'kind':'drill', 'payload':{'service':name}})
        await asyncio.sleep(.02 if name == '0' else .005)
        add_diagnosis(child, kind='dataplane', source='llm', observed=name, cause='test', status='up',
                      subject={'name':name}, evidence_ids=[eid])
        child.budget.dataplane_tools_used += 1
        session.tools_used += 1
        record['dataplane_status'] = 'up'
        active.remove(name)
        return True
    await run(case, selected, verify)
    assert order == ['0','2','1']
    assert peak == 2
    assert case.budget.dataplane_tools_used == 3
    evidence = {e['id']:e for e in case.evidence}
    assert len(evidence) == 4
    assert len({d['id'] for d in case.diagnoses}) == 3
    for d in case.diagnoses:
        assert evidence[d['evidence_ids'][0]]['payload']['service'] == d['subject']['name']
    assert all(rec['dataplane_status']=='up' for _,rec in selected)


@pytest.mark.asyncio
async def test_unknown_endpoints_exclusive_and_workers_one():
    for workers in (1, 3):
        active = []
        async def verify(client, settings, child, *, record, **kw):
            assert not (active and (not record['devices'] or None in active))
            active.append(record['devices'] or None)
            await asyncio.sleep(.001)
            active.remove(record['devices'] or None)
            return True
        await run(fixture(), candidates([], ['a'], ['b']), verify, workers)
    order=[]
    async def serial(client, settings, child, *, record, **kw):
        order.append(record['name']); await asyncio.sleep(.001); return True
    await run(fixture(), candidates(['a'], ['b']), serial, 1)
    assert order == ['0','1']


@pytest.mark.asyncio
async def test_extra_device_and_unscoped_reservations():
    sent=[]
    class Client:
        async def call_tool(self,n,p): sent.append((n,p)); return 'ok'
    reservations=Reservations(); reservations.active={0:{'a'},1:{'b'}}
    client=ReservedClient(Client(), reservations,0, {'name':'svc','service_type':'l2sts','devices':['a']})
    with pytest.raises(RuntimeError,match='not network-fault'):
        await client.call_tool('exec_show',{'device_name':'b'})
    with pytest.raises(RuntimeError):
        await client.call_tool('explore_nso_path',{'path':'tailf-ncs:devices'})
    assert not sent
    await client.call_tool('explore_nso_path',{'path':'tailf-ncs:devices/device=c/live-status'})
    assert reservations.active[0] == {'a','c'}
    assert not reservations.available({'c'})
    reservations.active.pop(1)
    await client.call_tool('compare_service_config',{'service_type':'l2sts','service_name':'different'})
    assert reservations.active[0] is None
    assert not reservations.available({'x'})


@pytest.mark.asyncio
async def test_provider_halt_stops_pending():
    case=fixture(); started=[]
    async def verify(client, settings, child, *, record, **kw):
        started.append(record['name'])
        if record['name']=='0':
            raise ProviderBudgetExceeded('budget')
        await asyncio.sleep(.001)
        return True
    await run(case,candidates(['a'],['b'],['c']),verify)
    assert '2' not in started
    assert case.service_coverage['2']=='llm_budget_exceeded'
    assert case.llm_halt_reason


@pytest.mark.asyncio
async def test_sync_llm_requests_overlap_in_workers():
    times=[]
    def request():
        start=time.monotonic(); time.sleep(.04); times.append((start,time.monotonic())); return 'ok'
    async def verify(client, settings, child, **kw):
        assert await llm_request(Investigation(),request)=='ok'
        return True
    await run(fixture(), candidates(['a'],['b']),verify)
    assert max(t[0] for t in times) < min(t[1] for t in times)


@pytest.mark.asyncio
async def test_failure_cancels_other_workers_and_releases_reservations():
    clients=[]; cancelled=asyncio.Event()
    async def verify(client, settings, child, *, record, **kw):
        clients.append(client)
        if record['name']=='0':
            await asyncio.sleep(.005); raise ValueError('unexpected')
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
    with pytest.raises(ValueError,match='unexpected'):
        await run(fixture(), candidates(['a'],['b']),verify)
    assert cancelled.is_set()
    assert all(not c.reservations.active for c in clients)


def test_cli_default_and_validation():
    from diagnostic_mas.run import build_parser
    parser=build_parser()
    assert parser.parse_args([]).dataplane_concurrent_works==1
    assert parser.parse_args(['--dataplane-concurrent_works','3']).dataplane_concurrent_works==3
    with pytest.raises(SystemExit): parser.parse_args(['--dataplane-concurrent_works','0'])


@pytest.mark.asyncio
async def test_reservation_rejection_is_not_a_wire_call(monkeypatch):
    import nso_facts.mcp_client as mcp
    counted=[]
    monkeypatch.setattr(mcp,'record_mcp_call',lambda *a,**k:counted.append(k))
    held=Reservations(); held.active={0:{'a'},1:{'b'}}
    client=ReservedClient(SimpleNamespace(),held,0,{'devices':['a']})
    with pytest.raises(RuntimeError,match='Concurrency reservation'):
        await mcp.call_mcp(client,'exec_show',{'device_name':'b','input_command':'interfaces'})
    assert not counted


@pytest.mark.asyncio
async def test_phase_parallel_path_refreshes_counts_and_coverage(monkeypatch):
    from diagnostic_mas import dataplane_verify as dv
    case=fixture()
    selected=candidates(['a'],['b'])
    for _,rec in selected:
        rec.update(system_status='up', dataplane_status='not_checked', status='up')
    services={rec['name']:rec for _,rec in selected}
    ev=selected[0][0]
    ev.update(kind='spine',role='service',payload={'extra':{'services':services}})
    monkeypatch.setattr(dv,'select_dataplane_candidates',lambda *a,**kw:selected)
    async def verify(client,settings,child,*,record,**kw):
        await asyncio.sleep(.001)
        dv._record_dataplane_finding(child,record,{'dataplane_status':'down', 'observed':'fault','cause':'fault'},source='llm')
        return True
    # Use the real recording path to verify merge -> coverage -> report counts.
    monkeypatch.setattr(dv,'llm_dataplane_verify_one',verify)
    await dv.run_dataplane_verify_phase(SimpleNamespace(),None,case,device_names={'a','b'},concurrent_works=2)
    assert len(case.diagnoses)==2
    assert all(case.service_coverage[r['name']]=='investigated' for _,r in selected)
    assert all(r['status']=='down' for _,r in selected)


@pytest.mark.asyncio
async def test_cdb_reads_do_not_compete_with_live_device_reservations():
    sent = []
    class Client:
        async def call_tool(self, name, params):
            sent.append((name, params))
            return 'ok'
    held = Reservations(); held.active = {0: {'a'}, 1: {'b'}}
    client = ReservedClient(Client(), held, 0,
                            {'name':'svc','service_type':'l3rt','devices':['a']})
    for path in ('/l3rt:l3rt=svc', 'tailf-ncs:services/l3rt:l3rt=svc/interface',
                 'l3rt:l3rt', 'tailf-ncs:devices/device=b/config/cisco:interface'):
        await client.call_tool('explore_nso_path', {'params': {'path':path}})
    await client.call_tool('get_device_config', {'device_name':'b'})
    assert len(sent) == 5
    assert held.active == {0:{'a'},1:{'b'}}
    for tool, params in (
        ('exec_show', {'device_name':'b'}),
        ('check_device_sync', {'device_name':'b'}),
        ('explore_nso_path', {'path':'devices/device=b/live-status'}),
        ('explore_nso_path', {'path':'tailf-ncs:devices'}),
        ('explore_nso_path', {'path':'unknown-root'}),
    ):
        with pytest.raises(RuntimeError):
            await client.call_tool(tool, params)
    assert len(sent) == 5


@pytest.mark.asyncio
async def test_cached_live_result_does_not_reserve_or_send(monkeypatch):
    import nso_facts.mcp_client as mcp
    params = {'device_name':'b','input_command':'interfaces'}
    result = {'result':'saved evidence'}
    token = mcp._mcp_cache.set({mcp._cache_key('exec_show', params): result})
    counted = []
    monkeypatch.setattr(mcp,'record_mcp_call',lambda *a,**kw: counted.append(kw))
    monkeypatch.setattr(mcp,'record_mcp_result',lambda *a,**kw: None)
    held = Reservations(); held.active = {0:{'a'},1:{'b'}}
    client = ReservedClient(SimpleNamespace(),held,0,{'devices':['a']})
    try:
        assert await mcp.call_mcp(client,'exec_show',params) == result
        assert counted == [{'cached':True}]
        assert held.active == {0:{'a'},1:{'b'}}
        with pytest.raises(RuntimeError):
            await mcp.call_mcp(client,'exec_show',{**params,'input_command':'version'})
    finally:
        mcp._mcp_cache.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('root', ['tailf-ncs:services', '/tailf-ncs:services/', 'ncs:services'])
@pytest.mark.parametrize('depth', [1, 2, 3])
async def test_service_discovery_root_during_parallel_digs(root, depth):
    sent = []
    class Client:
        async def call_tool(self, name, arguments):
            sent.append((name, arguments))
            return {'services': {}}
    held = Reservations(); held.active = {0: {'dall'}, 1: {'other'}}
    client = ReservedClient(Client(), held, 0,
                            {'service_type':'l3rt','devices':['dall']})
    await client.call_tool('explore_nso_path', {'params': {'path':root,'depth':depth}})
    assert len(sent) == 1
    assert held.active == {0: {'dall'}, 1: {'other'}}
    # The discovery exception must not grant access to another live device,
    # an unknown namespace, a broad device tree, or arbitrary service children.
    for path in ('tailf-ncs:devices/device=other/live-status',
                 'tailf-ncs:devices', 'unknown:services',
                 'tailf-ncs:services/unknown', 'tailf-ncs:services/../devices'):
        with pytest.raises(RuntimeError, match='Concurrency reservation'):
            await client.call_tool('explore_nso_path', {'path':path,'depth':depth})
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_worker_label_for_its_own_service_reaches_the_saved_case():
    case = fixture()
    selected = candidates(['a'], ['b'])
    case.service_coverage = {'0': 'needs_investigation', '1': 'needs_investigation'}

    async def verify(client, settings, child, *, record, **kwargs):
        if record['name'] == '0':
            child.service_coverage['0'] = 'disappeared'
            child.service_coverage['1'] = 'not this worker\'s to set'
        return True

    await run(case, selected, verify)

    assert case.service_coverage == {'0': 'disappeared', '1': 'needs_investigation'}

