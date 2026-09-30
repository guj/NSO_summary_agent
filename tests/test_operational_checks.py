import pytest
from diagnostic_mas.operational_checks.l2ptp import evaluate
from diagnostic_mas.case import CaseFile, Budget
from diagnostic_mas.dataplane_verify import select_dataplane_candidates


def record():
    return dict(name="circuit", service_type="l2ptp", devices=["a", "b"],
                system_status="up", status="up", in_sync=None,
                dataplane_status="not_checked", device_sync={"a": "in-sync", "b": "in-sync"},
                live_l2={"endpoints": [dict(device=d, ac="Hu0.100", st="UP", xconnect="xc", ac_st="UP", seg2_st="UP") for d in ("a", "b")]})


def test_all_endpoints_required_and_missing_is_not_down():
    r = record()
    assert evaluate(r)["status"] == "up"
    r["live_l2"]["endpoints"].pop()
    assert evaluate(r)["status"] == "unknown"
    r["live_l2"]["endpoints"][0]["st"] = "DN"
    assert evaluate(r)["status"] == "down"


def test_sync_prerequisite_and_force():
    r = record()
    r["device_sync"]["b"] = None
    assert evaluate(r)["status"] == "not_checked"
    assert evaluate(r, force=True)["status"] == "up"
    assert not evaluate(r, force=True)["sync_ready"]


def test_selection_skips_pass_but_retains_fault_and_force():
    r = record()
    r["basic_checks"] = evaluate(r)
    c = CaseFile(Budget(0, 0))
    c.evidence = [{"kind": "spine", "role": "service", "payload": {"extra": {"services": {"l2ptp/circuit": r}}}}]
    # Accommodate actual evidence envelope used by the runner.
    c.evidence[0]["result"] = c.evidence[0]["payload"]
    assert not select_dataplane_candidates(c)
    assert len(select_dataplane_candidates(c, explicit_service=True)) == 1
    r["live_l2"]["endpoints"][0]["st"] = "DN"
    r["basic_checks"] = evaluate(r)
    assert len(select_dataplane_candidates(c)) == 1


def test_segment_failure_and_tool_error():
    r = record()
    r["live_l2"]["endpoints"][0]["seg2_st"] = "DN"
    assert evaluate(r)["status"] == "down"
    r = record()
    r["live_l2"]["endpoints"][0]["error"] = "timeout"
    assert evaluate(r)["status"] == "unknown"


@pytest.mark.asyncio
async def test_collection_reuses_devices_and_skips_sync_unknown(monkeypatch):
    from nso_facts.service_collect import collect_service_health
    import nso_facts.service_collect as sc
    calls = []
    async def fake_call(client, tool, args=None):
        calls.append((tool, args))
        return {"status": "success", "data": {"result": "evpn_vpws xc\n UP Hu0/0/0/1.100 UP EVPN 9001,100,10.0.0.1 UP"}}
    monkeypatch.setattr(sc, "call_mcp", fake_call)
    def instance(name, remote):
        def endpoint(d):
            return {"device": d, "interface": {"type": "HundredGigE", "id": "0/0/0/1", "outervlan": 100}}
        return {"name": name, "stp-a": endpoint("a"), "stp-z": endpoint(remote)}
    services = {"l2ptp": {"status": "success", "data": {"services": [instance("one", "b"), instance("two", "b"), instance("skip", "c")]}}}
    out = await collect_service_health(None, services, {}, {"a": "in-sync", "b": "in-sync", "c": "error: timeout"}, service_sync_mode="skip", operational_policy="routine")
    assert [args["device_name"] for tool, args in calls] == ["a", "b"]
    assert out["l2ptp/one"]["basic_checks"]["status"] == "up"
    assert out["l2ptp/two"]["basic_checks"]["status"] == "up"
    assert out["l2ptp/skip"]["basic_checks"]["status"] == "not_checked"
    assert out["l2ptp/one"]["dataplane_status"] == "not_checked"
