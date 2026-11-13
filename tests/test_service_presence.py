from unittest.mock import AsyncMock
import pytest
from diagnostic_mas.case import CaseFile, Budget
from diagnostic_mas import service_presence as sp
from diagnostic_mas.service_final_status import final_service_counts
from diagnostic_mas.operator_report import scope_counts, format_followup_operator


@pytest.mark.asyncio
@pytest.mark.parametrize("response,outcome",[
 ({"status":"success","data":{"services":[]}},"absent"),
 ({"status":"success","data":{"services":[{"name":"svc"}]}},"present"),
 ({"status":"error","data":{"services":[]}},"unknown"),
 ({"status":"success","data":{}},"unknown"),
 ({"status":"success","data":{"services":[],"has_more":True}},"unknown"),
 ({"status":"success","data":{"services":[{}]}},"unknown"),
])
async def test_presence_recheck(monkeypatch,response,outcome):
    call=AsyncMock(return_value=response);monkeypatch.setattr(sp,"call_mcp",call)
    r={"name":"svc","service_type":"l2bridge","devices":["a"],"basic_checks":{"checked_at":"earlier"}}
    c=CaseFile(Budget(0,0));c.evidence=[{"kind":"spine","role":"service","payload":{"extra":{"services":{"l2bridge/svc":r}}}}]
    c.issues=[{"layer":"services","edge_id":"svc","code":"service_down","status":"open"}]
    f={"dataplane_status":"down","cause":"Service objects absent","fix_suggestion":"restore"}
    out=await sp.reconcile_presence(None,c,r,f)
    call.assert_awaited_once_with(None,"get_services",{"service_type":"l2bridge"},bypass_cache=True)
    assert r["presence_recheck"]["outcome"]==outcome
    if outcome=="absent":
        assert out["service_disappeared"]
        assert final_service_counts(c)=={}
        assert scope_counts(c)[1]==0
        assert "Services no longer present" in "\n".join(sp.disappearance_section(c))
        from diagnostic_mas.operator_report import _services_to_render
        assert _services_to_render(c, services_detail=True)==[]
        assert _services_to_render(c, services_detail=False)==[]
        assert "collection-reported fault" not in "\n".join(format_followup_operator(c))
    elif outcome=="present": assert out==f
    else:
        assert out["dataplane_status"]=="unknown"
        assert out["fix_suggestion"] is None
        assert final_service_counts(c)["l2bridge"]["total"]==1


@pytest.mark.asyncio
async def test_inventory_exception_is_not_deletion(monkeypatch):
    monkeypatch.setattr(sp,"call_mcp",AsyncMock(side_effect=TimeoutError))
    r={"name":"svc","service_type":"l2sts"};c=CaseFile(Budget(0,0))
    out=await sp.reconcile_presence(None,c,r,{"dataplane_status":"down","cause":"service absent"})
    assert out["dataplane_status"]=="unknown" and not sp.disappeared(r)


@pytest.mark.asyncio
async def test_normal_link_fault_does_not_refresh_inventory(monkeypatch):
    call=AsyncMock();monkeypatch.setattr(sp,"call_mcp",call)
    f={"dataplane_status":"down","cause":"Attachment link down, receive power low"}
    assert await sp.reconcile_presence(None,CaseFile(Budget(0,0)),{},f)==f
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_gone_before_its_investigation_is_recorded_as_no_longer_present(monkeypatch):
    call=AsyncMock(return_value={"status":"success","data":{"services":[{"name":"other"}]}})
    monkeypatch.setattr(sp,"call_mcp",call)
    r={"name":"svc","service_type":"l2bridge","devices":["a"],"basic_checks":{"checked_at":"earlier"}}
    c=CaseFile(Budget(0,0));c.evidence=[{"kind":"spine","role":"service","payload":{"extra":{"services":{"l2bridge/svc":r}}}}]
    c.issues=[{"layer":"services","edge_id":"svc","code":"service_down","status":"open"}]

    assert await sp.gone_before_investigation(None,c,r) is True
    call.assert_awaited_once_with(None,"get_services",{"service_type":"l2bridge"},bypass_cache=True)
    assert sp.disappeared(r) and c.service_coverage["svc"]=="disappeared"
    assert c.issues[0]["status"]=="explained"
    assert "Services no longer present" in "\n".join(sp.disappearance_section(c))
    assert final_service_counts(c)=={}


@pytest.mark.asyncio
@pytest.mark.parametrize("response",[
 {"status":"success","data":{"services":[{"name":"svc"}]}},
 {"status":"error","data":{"services":[]}},
 {"status":"success","data":{"services":[],"has_more":True}},
 TimeoutError(),
])
async def test_service_still_listed_or_unreadable_list_lets_the_investigation_run(monkeypatch,response):
    if isinstance(response,Exception): call=AsyncMock(side_effect=response)
    else: call=AsyncMock(return_value=response)
    monkeypatch.setattr(sp,"call_mcp",call)
    r={"name":"svc","service_type":"l2bridge"};c=CaseFile(Budget(0,0))

    assert await sp.gone_before_investigation(None,c,r) is False
    assert "presence_recheck" not in r and c.evidence==[] and "svc" not in c.service_coverage


def _case_with(*names, kind="l2bridge"):
    c = CaseFile(Budget(0, 0))
    records = {f"{kind}/{n}": {"name": n, "service_type": kind, "devices": ["a"], "basic_checks": {"checked_at": "earlier"}}
               for n in names}
    c.evidence = [{"kind": "spine", "role": "service", "payload": {"extra": {"services": records}}}]
    c.issues = [{"layer": "services", "edge_id": n, "code": "service_down", "status": "open"} for n in names]
    return c, [(c.evidence[0], r) for r in records.values()]


@pytest.mark.asyncio
async def test_services_gone_before_the_dataplane_step_are_replaced_by_the_next_ranked_ones(monkeypatch):
    call = AsyncMock(return_value={"status": "success", "data": {"services": [{"name": "c"}, {"name": "d"}]}})
    monkeypatch.setattr(sp, "call_mcp", call)
    case, ranked = _case_with("a", "b", "c", "d", "e")

    kept = await sp.keep_services_still_listed(None, case, ranked, limit=2)

    assert [r["name"] for _ev, r in kept] == ["c", "d"]
    call.assert_awaited_once_with(None, "get_services", {"service_type": "l2bridge"}, bypass_cache=True)   # one listing per type
    assert case.service_coverage == {"a": "disappeared", "b": "disappeared"}
    assert all(i["status"] == "explained" for i in case.issues if i["edge_id"] in {"a", "b"})
    assert all(i["status"] == "open" for i in case.issues if i["edge_id"] not in {"a", "b"})


@pytest.mark.asyncio
async def test_a_listing_that_fails_does_not_drop_candidates(monkeypatch):
    monkeypatch.setattr(sp, "call_mcp", AsyncMock(side_effect=TimeoutError))
    case, ranked = _case_with("a", "b", "c")

    kept = await sp.keep_services_still_listed(None, case, ranked, limit=2)

    assert [r["name"] for _ev, r in kept] == ["a", "b"] and case.service_coverage == {}


@pytest.mark.asyncio
async def test_each_service_type_is_listed_once_and_the_cap_is_never_exceeded(monkeypatch):
    async def listing(_client, _tool, args, **_kw):
        return {"status": "success", "data": {"services": [{"name": n} for n in ("a", "x")]}}
    call = AsyncMock(side_effect=listing)
    monkeypatch.setattr(sp, "call_mcp", call)
    case, ranked = _case_with("a", "b", "c")
    case2, ranked2 = _case_with("x", "y", kind="l3rt")

    kept = await sp.keep_services_still_listed(None, case, ranked + ranked2, limit=3)

    assert [r["name"] for _ev, r in kept] == ["a", "x"]            # b, c and y are gone; nothing left to fill the cap
    assert call.await_count == 2 and len(kept) <= 3


def _case_with_statuses(statuses, kind="l2bridge"):
    """A case whose services have the given fixed-check statuses ({name: "down" | "up" | ...})."""
    c = CaseFile(Budget(0, 0))
    records = {f"{kind}/{n}": {"name": n, "service_type": kind, "devices": ["a"], "system_status": "up",
                               "device_sync": {"a": "in-sync"}, "basic_checks": {"checked_at": "earlier", "status": s, "needs_investigation": False}}
               for n, s in statuses.items()}
    c.evidence = [{"kind": "spine", "role": "service", "payload": {"extra": {"services": records}}}]
    c.issues = [{"layer": "services", "edge_id": n, "code": "service_down", "status": "open"}
                for n, s in statuses.items() if s == "down"]
    return c, records


@pytest.mark.asyncio
async def test_a_service_reported_down_that_a_fresh_listing_no_longer_has_is_not_reported(monkeypatch):
    call = AsyncMock(return_value={"status": "success", "data": {"services": [{"name": "kept"}, {"name": "fine"}]}})
    monkeypatch.setattr(sp, "call_mcp", call)
    case, records = _case_with_statuses({"gone": "down", "kept": "down", "fine": "up", "also-gone-but-up": "up"})

    await sp.recheck_reported_faults(None, case)

    call.assert_awaited_once_with(None, "get_services", {"service_type": "l2bridge"}, bypass_cache=True)
    assert sp.disappeared(records["l2bridge/gone"]) and case.service_coverage == {"gone": "disappeared"}
    assert not sp.disappeared(records["l2bridge/kept"])
    # A service reported Up is not rechecked: its deletion is for the next scan to show.
    assert not sp.disappeared(records["l2bridge/also-gone-but-up"])
    assert final_service_counts(case)["l2bridge"]["down"] == 1
    assert [i["status"] for i in case.issues if i["edge_id"] == "gone"] == ["explained"]
    assert "gone" in "\n".join(sp.disappearance_section(case))


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"status": "error", "data": {"services": []}},
    {"status": "success", "data": {"services": [], "has_more": True}},
    TimeoutError(),
])
async def test_a_listing_that_cannot_be_read_leaves_a_reported_fault_alone(monkeypatch, response):
    monkeypatch.setattr(sp, "call_mcp", AsyncMock(side_effect=[response]) if isinstance(response, Exception)
                        else AsyncMock(return_value=response))
    case, records = _case_with_statuses({"svc": "down"})

    await sp.recheck_reported_faults(None, case)

    assert not sp.disappeared(records["l2bridge/svc"]) and case.service_coverage == {}
    assert final_service_counts(case)["l2bridge"]["down"] == 1


@pytest.mark.asyncio
async def test_only_service_types_with_a_reported_fault_are_listed_and_each_once(monkeypatch):
    call = AsyncMock(return_value={"status": "success", "data": {"services": [{"name": "a"}, {"name": "b"}]}})
    monkeypatch.setattr(sp, "call_mcp", call)
    case, _ = _case_with_statuses({"a": "down", "b": "down"})
    other, records = _case_with_statuses({"x": "up"}, kind="l3rt")
    case.evidence += other.evidence

    await sp.recheck_reported_faults(None, case)

    call.assert_awaited_once_with(None, "get_services", {"service_type": "l2bridge"}, bypass_cache=True)
    assert not sp.disappeared(records["l3rt/x"])


@pytest.mark.asyncio
async def test_with_no_reported_fault_nothing_is_listed(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(sp, "call_mcp", call)
    case, _ = _case_with_statuses({"a": "up", "b": "unknown"})

    await sp.recheck_reported_faults(None, case)

    call.assert_not_awaited()
