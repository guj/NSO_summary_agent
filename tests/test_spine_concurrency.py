import asyncio
from collections import Counter
import pytest
from diagnostic_mas.operational_checks.scheduler import DeviceCalls, ProbeCache, run_groups
from diagnostic_mas.operational_checks.probe import Probe


@pytest.mark.asyncio
async def test_device_serialization_and_global_limit():
    active = Counter()
    peak = 0
    async def wire(client, tool, params):
        nonlocal peak
        dev = params['device_name']
        active[dev] += 1
        assert active[dev] == 1
        peak = max(peak, sum(active.values()))
        await asyncio.sleep(.005)
        active[dev] -= 1
        return {'status':'success','data':{'result':'ok'}}
    call = DeviceCalls(wire, 2)
    await asyncio.gather(*(call(None,'exec_show',{'device_name':d,'input_command':str(i)}) for i,d in enumerate(['a','a','b','c','c','b'])))
    assert peak == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('response', ['ok', ''])
async def test_shared_probe_coalesces_success_and_failure(response):
    count=0
    async def wire(client,tool,params):
        nonlocal count
        count+=1
        await asyncio.sleep(.005)
        return {'status':'success','data':{'result':response}}
    cache=ProbeCache()
    probes=[Probe(None,DeviceCalls(wire,4),cache) for _ in range(8)]
    assert await asyncio.gather(*(p.show('a','interfaces brief') for p in probes)) == [response]*8
    assert count==1
    assert sum(p.calls for p in probes)==1
    assert all(len(p.sources)==1 for p in probes)


@pytest.mark.asyncio
async def test_serial_order_and_worker_exception_cleanup():
    seen=[]
    async def action(item):
        seen.append(item)
    await run_groups(['a','b','a'],action,lambda x:x,1)
    assert seen==['a','b','a']
    active=set()
    async def failure(item):
        active.add(item)
        try:
            if item=='b': raise RuntimeError('test')
            await asyncio.sleep(1)
        finally:
            active.remove(item)
    with pytest.raises(ExceptionGroup):
        await run_groups(['a','b'],failure,lambda x:x,2)
    assert not active


def test_report_records_spine_default_and_override(tmp_path):
    from types import SimpleNamespace
    from test_diagnostic_mas_publish import _settings
    from diagnostic_mas.run_configuration import capture_run_configuration
    from diagnostic_mas.case import Budget
    settings=_settings(state_dir=tmp_path)
    for args,n in [(SimpleNamespace(),1),(SimpleNamespace(spine_concurrent_devices=4),4)]:
        assert capture_run_configuration(args,settings,Budget(0,0),skip_llm=True,dry_run=True)['Concurrent spine device calls']==n
