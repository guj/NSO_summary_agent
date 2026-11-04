from types import SimpleNamespace
import pytest
from nso_facts.sync_recheck import recheck_sync


@pytest.mark.asyncio
async def test_one_retry_and_explicit_results_only():
    calls=[]
    async def call(client, tool, params):
        name=params['device_name']; calls.append(name)
        if name=='bad': raise TimeoutError('sync timeout')
        return {'status':'success','data':{'sync_state': {'a':'in-sync','b':'out-of-sync'}.get(name)}}
    initial={'a':'error','b':'unknown','bad':'error','null':'error','ok':'in-sync','out':'out-of-sync','lock':'locked'}
    effective,audit=await recheck_sync(None,initial,initial,call=call)
    assert calls==['a','b','bad','null']
    assert effective['a']=='in-sync' and effective['b']=='out-of-sync'
    assert effective['bad']==effective['null']=='error'
    assert initial['a']=='error'
    assert audit['bad']['retry_result']['error']=='sync timeout'
    assert not audit['null']['resolved']


@pytest.mark.asyncio
async def test_recheck_updates_service_counts_and_audit(monkeypatch):
    from nso_facts.fact_pack import collect_services_fact_slice
    from diagnostic_mas.case import CaseFile,Budget,add_evidence
    from diagnostic_mas.report import render_report
    calls=[]
    async def call(client,tool,params=None):
        calls.append(tool)
        if tool=='get_fleet_sync_summary':
            return {'status':'success','data':{'devices':[{'device':'a','result':'error'}]}}
        if tool=='get_services':
            return {'status':'success','data':{'services':[{'name':'svc','endpoint':{'device':'a'}}]}}
        if tool=='check_device_sync':
            return {'status':'success','data':{'sync_state':'in-sync'}}
        raise AssertionError(tool)
    monkeypatch.setattr('nso_facts.fact_pack.call_mcp',call)
    settings=SimpleNamespace(ignore_service_types=frozenset(),max_service_types=10,service_sync_mode='skip')
    pack=await collect_services_fact_slice(None,settings,only_service_types=['l3rt'],retry_inconclusive_sync=True)
    assert calls.count('check_device_sync')==1
    assert pack['fleet_sync']['data']['devices'][0]['result']=='error'
    assert next(iter(pack['services'].values()))['device_sync']['a']=='in-sync'
    case=CaseFile(budget=Budget(0,0))
    add_evidence(case,{'kind':'spine','role':'service','payload':{'extra':pack}})
    assert 'fleet `error` → retry in-sync' in render_report(case)


@pytest.mark.asyncio
async def test_error_false_is_unknown_and_services_group_once():
    from nso_facts.health import parse_in_sync
    from diagnostic_mas.case import CaseFile, Budget
    from diagnostic_mas.sync_failure_report import format_sync_failure_group, sync_failure_group
    from diagnostic_mas.operator_report import format_followup_operator, _sync_prose
    from diagnostic_mas.service_final_status import final_service_counts
    from diagnostic_mas.operational_checks.common import sync_ready
    async def call(*args):
        return {"status":"success","data":{"in_sync":False,"result":"error","outcome":"failed"}}
    effective,audit=await recheck_sync(None,{"utah":"error","wash":"error"},["utah","wash"],call=call)
    assert effective=={"utah":"error","wash":"error"}
    assert all(not r["resolved"] for r in audit.values())
    services={}
    for name,devices in [("a",["utah"]),("b",["wash"]),("both",["utah","wash"])]:
        services["l2sts/"+name]={"name":name,"service_type":"l2sts","devices":devices,
            "device_sync":{d:effective[d] for d in devices},"system_status":"unknown","status":"unknown"}
    assert all(not sync_ready(r) for r in services.values())
    c=CaseFile(Budget(0,0));c.evidence=[{"kind":"spine","role":"service","payload":{"extra":{"services":services,"sync_rechecks":audit}}}]
    assert sync_failure_group(c)==(["utah","wash"],3)
    assert final_service_counts(c)["l2sts"]["unknown"]==3
    group="\n".join(format_sync_failure_group(c))
    assert "3 distinct services" in group
    assert "devices device utah check-sync" in group and "devices device wash check-sync" in group
    follow="\n".join(format_followup_operator(c))
    assert follow.count("Resolve failed sync verification")==1
    assert "Restore live collection evidence for `utah`" not in follow
    assert _sync_prose("error").startswith("Unknown")
    assert parse_in_sync({"status":"success","data":{"in_sync":False,"result":"out-of-sync","outcome":"failed"}}) is False
    assert parse_in_sync({"status":"success","data":{"in_sync":True,"result":"in-sync"}}) is True


@pytest.mark.asyncio
async def test_a_quarantined_device_is_not_retried_and_its_audit_says_so(monkeypatch):
    calls=[]
    async def call(client, tool, params):
        calls.append(params['device_name'])
        return {'status':'success','data':{'in_sync':False,'result':'error','outcome':'failed'}}
    monkeypatch.setattr('nso_facts.mcp_client.is_device_quarantined', lambda device: device=='star')
    effective,audit=await recheck_sync(None,{'star':'error','cien':'error'},['star','cien'],call=call)
    assert calls==['cien']
    assert effective['star']=='error'
    assert audit['star']['skipped']=='device quarantined this run' and not audit['star']['resolved']
    assert 'skipped' not in audit['cien']


_STAR_REFUSED = ("check_isis_adjacencies: RESTCONF 500: Failed to connect to device star-data-sw: connection refused: "
                 "NEDCOM CONNECT: authenticate: Socket closed, server allowed: [ ] in new state")
_CIEN_TIMED_OUT = ("get_hardware_health: RESTCONF 404: Failed to connect to device cien-data-sw: connection refused: "
                   "Failed to connect: Connect timed out")


def _sync_failed_case(monkeypatch):
    """test32's shape: NSO could not connect to cien (retried) or star (quarantined first); utah answered but its sync stayed unread."""
    from diagnostic_mas.case import CaseFile, Budget, add_evidence
    from diagnostic_mas.ingest import ingest_quarantined_devices
    sync = {"cien-data-sw":"error","star-data-sw":"error","utah-data-sw":"error"}
    services = {}
    for name, devices in [("a",["star-data-sw"]),("b",["star-data-sw","cien-data-sw"]),("c",["cien-data-sw"]),
                          ("d",["utah-data-sw","star-data-sw"]),("e",["utah-data-sw"])]:
        services["l2sts/"+name] = {"name":name,"service_type":"l2sts","devices":devices,
            "device_sync":{d:sync[d] for d in devices},"system_status":"unknown","status":"unknown"}
    unresolved = {"fleet_result":"error","effective_result":"error","resolved":False}
    audit = {"cien-data-sw":{**unresolved,"retry_result":{"status":"success","data":{"in_sync":False,"result":"error"}}},
             "star-data-sw":{**unresolved,"retry_result":None,"skipped":"device quarantined this run"},
             "utah-data-sw":{**unresolved,"retry_result":{"status":"success","data":{"in_sync":False,"result":"error"}}}}
    case = CaseFile(budget=Budget(0,0))
    add_evidence(case, {"kind":"spine","role":"service","layer":"services",
                        "payload":{"extra":{"services":services,"sync_rechecks":audit}}})
    monkeypatch.setattr("nso_facts.mcp_client.quarantined_devices",
                        lambda: {"cien-data-sw":_CIEN_TIMED_OUT,"star-data-sw":_STAR_REFUSED})
    ingest_quarantined_devices(case)
    return case


def test_a_skipped_sync_retry_is_not_reported_as_run(monkeypatch):
    from diagnostic_mas.report import render_report
    from diagnostic_mas.sync_failure_report import format_sync_failure_group
    case = _sync_failed_case(monkeypatch)

    report = render_report(case)
    group = "\n".join(format_sync_failure_group(case))

    assert "- star-data-sw: fleet `error` → retry skipped (device quarantined this run); Sync Unknown retained." in report
    assert "- cien-data-sw: fleet `error` → retry inconclusive; Sync Unknown retained." in report
    assert "(the retry was skipped on `star-data-sw`, quarantined this run)" in group


def test_devices_nso_could_not_connect_to_get_connect_not_check_sync(monkeypatch):
    from diagnostic_mas.operator_report import format_followup_operator
    from diagnostic_mas.sync_failure_report import format_sync_failure_group
    case = _sync_failed_case(monkeypatch)

    group = "\n".join(format_sync_failure_group(case))
    follow = "\n".join(format_followup_operator(case))

    assert "devices device cien-data-sw connect" in group and "devices device star-data-sw connect" in group
    assert "devices device utah-data-sw check-sync" in group
    assert "star-data-sw check-sync" not in group and "cien-data-sw check-sync" not in group
    # Services behind a device NSO could not reach are counted there; the rest with check-sync.
    assert ("Check NSO's management connection to `cien-data-sw` and `star-data-sw` to settle 4 services "
            "left Unknown: NSO's own attempts to connect to these devices failed, so their sync could not be read; "
            "`devices device cien-data-sw connect`; `devices device star-data-sw connect` in the NSO CLI show "
            "whether NSO can reach them; run `check-sync` once they connect. These are not confirmed outages.") in follow
    assert "Resolve failed sync verification for 1 distinct services" in follow
    assert "`devices device utah-data-sw check-sync`" in follow
    assert "star-data-sw check-sync" not in follow
    assert follow.count("management connection to `star-data-sw`") == 0
