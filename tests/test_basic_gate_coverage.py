"""Basic evidence closes coverage gaps without overriding dig failures."""
from copy import deepcopy
import pytest
from diagnostic_mas.dataplane_verify import accept_dataplane_conclusion


def record():
    return {"name":"svc", "service_type":"l2sts", "devices":["a","b"],
        "basic_checks":{"schema_version":1, "service_id":"svc", "service_type":"l2sts",
            "devices":["a","b"], "sync_ready":True, "status":"unknown",
            "checked_at":"2026-09-28T10:00:00+00:00",
            "sources":[{"device":d,"tool":"exec_show","command":"evpn evi vpn-id 1 detail",
                "available":True,"collected_at":"2026-09-28T09:59:00+00:00"} for d in ["a","b"]],
            "checks":[{"device":d,"check":"effective_evpn","status":"pass"} for d in ["a","b"]]}}


def finding():
    return {"dataplane_status":"up","observed":"Both scoped forwarding paths verified.","cause":"No PE fault found."}


def session(error=None):
    payload={"check":"exec_show","args":{"device_name":"b","input_command":"cef 10.0.0.1"},"result":"resolved labeled path"}
    if error: payload["error"]=error
    return [{"kind":"drill","payload":payload}]


def test_basic_coverage_avoids_requerying_other_endpoint():
    assert accept_dataplane_conclusion(record(),finding(),session_evidence=session())["dataplane_status"]=="up"


@pytest.mark.parametrize("change", ["identity","type","devices","timestamp","unavailable","future_source","no_checks","sync"])
def test_unusable_basic_evidence_cannot_cover_missing_endpoint(change):
    r=record(); b=r["basic_checks"]
    if change=="identity": b["service_id"]="different-service"
    if change=="type": b["service_type"]="l3rt"
    if change=="devices": b["devices"]=["b","c"]
    if change=="timestamp": b.pop("checked_at")
    if change=="unavailable": b["sources"][0]["available"]=False
    if change=="future_source": b["sources"][0]["collected_at"]="2026-09-29T00:00:00+00:00"
    if change=="no_checks": b["checks"]=[]
    if change=="sync": b["sync_ready"]=False
    out=accept_dataplane_conclusion(r,finding(),session_evidence=session())
    assert out["dataplane_status"]=="unknown"
    assert "required endpoints" in out["cause"]


def test_generic_basic_success_is_not_configuration_proof():
    r=record()
    for c in r["basic_checks"]["checks"]: c["check"]="attachment"
    out=accept_dataplane_conclusion(r,finding(),session_evidence=session())
    assert out["dataplane_status"]=="unknown"
    assert "config-like" in out["cause"]


def test_new_tool_failure_still_blocks_up():
    out=accept_dataplane_conclusion(record(),finding(),session_evidence=session("timed out"))
    assert out["dataplane_status"]=="unknown"
    assert "necessary check failed" in out["cause"]


def test_forwarding_gate_remains_active():
    f=finding(); f["observed"]="Only one direction verified; reverse forwarding remains unverified."
    out=accept_dataplane_conclusion(record(),f,session_evidence=session())
    assert out["dataplane_status"]=="unknown"
