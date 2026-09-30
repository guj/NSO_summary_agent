import json
from pathlib import Path
import pytest
from diagnostic_mas.dataplane_verify import accept_dataplane_conclusion


def sample():
    return json.loads((Path(__file__).parent / "fixtures/losa_replication_gate.json").read_text())


def assess(s):
    return accept_dataplane_conclusion(s["record"],s["finding"],session_evidence=s["evidence"])


def test_saved_losa_replication_is_accepted_with_limited_scope():
    out=assess(sample())
    assert out["dataplane_status"]=="up"
    assert "learned-unicast MAC forwarding and customer traffic delivery were not verified" in out["observed"]


@pytest.mark.parametrize("missing",["replication","rt_compatibility","attachment","membership","local_forwarding","local_flooding","effective_evpn","transport"])
def test_missing_basic_component_cannot_pass(missing):
    s=sample(); checks=s["record"]["basic_checks"]["checks"]
    checks[:]=[c for c in checks if not (c["device"]=="losa-data-sw" and c["check"]==missing)]
    assert assess(s)["dataplane_status"]=="unknown"


@pytest.mark.parametrize("change",["remove","wrong_target","unresolved","latest_failure"])
def test_transport_gap_requires_successful_exact_remote_proof(change):
    s=sample()
    target=next(e for e in s["evidence"] if e["id"]=="ev_284")
    if change=="remove": s["evidence"].remove(target)
    if change=="wrong_target": target["payload"]["args"]["input_command"]="cef 10.137.0.1/32"
    if change=="unresolved": target["payload"]["result"]="unresolved"
    if change=="latest_failure":
        later=json.loads(json.dumps(target));later["payload"]["result"]="unresolved";s["evidence"].append(later)
    assert assess(s)["dataplane_status"]=="unknown"


def test_positive_basic_fault_cannot_be_ignored():
    s=sample(); s["record"]["basic_checks"]["checks"][0]["status"]="fault"
    assert assess(s)["dataplane_status"]=="unknown"
