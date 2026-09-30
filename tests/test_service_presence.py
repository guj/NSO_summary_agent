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
