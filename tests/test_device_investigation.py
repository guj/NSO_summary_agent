"""A device cut off from the network is investigated once, before its services."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from diagnostic_mas import port_investigation as pi
from diagnostic_mas.case import Budget, CaseFile, add_evidence, debit_drill, open_issue
from diagnostic_mas.drill import record_drill_finding

PE, HUB, FAR, MUTE = "pe-data-sw", "hub-data-sw", "far-data-sw", "mute-data-sw"
IDLE = {"local": "idle", "remote": "idle", "status": "down"}
UP = {"local": "established", "remote": "established", "status": "up"}


def _service(name: str, kind: str, devices: list[str], status: str) -> dict:
    return {
        "name": name, "service_type": kind, "devices": devices,
        "in_sync": True, "device_sync": {d: "in-sync" for d in devices},
        "system_status": "up", "dataplane_status": "not_checked", "status": status,
        "basic_checks": {"status": status, "needs_investigation": status != "up",
                         "checks": [{"check": "rib", "status": "unknown" if status != "up" else "pass",
                                     "device": devices[0], "observation": "route"}]},
    }


def _case(*services: dict, bgp: dict | None = None, isis: dict | None = None,
          issues: tuple[dict, ...] = ()) -> CaseFile:
    """PE's two sessions to the hub are idle on both ends and it has no adjacency."""
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = [PE, HUB, FAR, MUTE]
    bgp = bgp if bgp is not None else {
        f"bgp:10.0.0.1:10.0.1.1:{HUB}:{PE}": IDLE, f"bgp:10.0.0.2:10.0.1.1:{HUB}:{PE}": IDLE,
        f"bgp:10.0.0.1:10.0.2.1:{HUB}:{FAR}": UP}
    isis = isis if isis is not None else {f"isis:{HUB}:Hu0/0/0/1:{FAR}:Hu0/0/0/2": {"status": "up"}}
    for role, edges in (("bgp", bgp), ("isis", isis)):
        add_evidence(case, {"kind": "spine", "role": role, "payload": {
            "operational_edges": [{"id": edge, "state": dict(state)} for edge, state in edges.items()]}})
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services", "payload": {"extra": {
        "services": {f"{s['service_type']}/{s['name']}": s for s in services}}}})
    for s in services:
        if s["status"] != "up":
            open_issue(case, code=f"service_{s['status']}", message=f"{s['name']} {s['status']}",
                       evidence_ids=[], layer="services", edge_id=s["name"], devices=list(s["devices"]))
    case.issues.extend(dict(i) for i in issues)
    return case


def _pe_case(**kwargs) -> CaseFile:
    return _case(
        _service("two-site", "l2sts", [PE, FAR], "unknown"),       # its own config names another device
        _service("external", "l3rt", [PE, HUB], "unknown"),        # names its border router
        _service("local-routed", "l3rt", [PE], "up"),              # config is silent about other sites
        _service("local-unknown", "l3rt", [PE], "unknown"),        # one device: not explained by the cut-off
        _service("elsewhere", "l2sts", [HUB, FAR], "unknown"),     # not on the cut-off device
        **kwargs)


SUMMARY = """BGP router identifier 10.0.1.1, local AS number 398900
Neighbor        Spk    AS MsgRcvd MsgSent   TblVer  InQ OutQ  Up/Down  St/PfxRcd
10.0.0.1          0 398900       0       0        0    0    0 00:00:00 {first}
10.0.0.2          0 398900       0       0        0    0    0 00:00:00 Idle
"""


def _loop(calls: list[str], *, conclude: bool = True, sessions: str | None = "Idle"):
    """``sessions``: state of the first neighbour in the summary it reads; None reads no summary."""
    async def fake(client, settings, case, *, device_names, focus_issue, session,
                   openai_client=None, system_prompt=None, account="drill"):
        calls.append(focus_issue["edge_id"])
        assert "lost its connection" in system_prompt and account == "port"
        debit_drill(case, 4, session=session, account="port")
        if sessions is not None:
            add_evidence(case, {"kind": "drill", "role": "drill", "layer": "services", "payload": {
                "check": "exec_show", "issue_edge_id": session.issue_edge_id,
                "args": {"device_name": PE, "input_command": "bgp summary"},
                "result": {"result": SUMMARY.format(first=sessions)}}})
        if conclude:
            record_drill_finding(case, {
                "observed": "uplink Hu0/0/0/23 down/down", "cause": "the only uplink is down",
                "fix_suggestion": "check the uplink fibre", "fix_requires_human_approval": True,
                "confidence": "high"}, session=session)
        return 4, conclude
    return fake


async def _run(case: CaseFile, loop, answered=(PE, HUB, FAR)) -> set[str]:
    return await pi.run_device_investigations(
        None, SimpleNamespace(), case, device_names=set(case.device_names),
        answered=set(answered), tool_loop=loop)


def test_cut_off_device_answered_and_has_no_session_or_adjacency_up():
    case = _pe_case()

    assert pi.cut_off_devices(case, answered={PE, HUB, FAR}) == [
        {"device": PE, "sessions_down": [f"bgp:10.0.0.1:10.0.1.1:{HUB}:{PE}",
                                         f"bgp:10.0.0.2:10.0.1.1:{HUB}:{PE}"]}]


def test_device_is_not_called_cut_off_without_its_own_answer_or_with_anything_up():
    failed = {"layer": "underlay", "code": "collection_error", "status": "open",
              "message": f"{PE}: check_isis_adjacencies failed: timeout"}
    one_up = {f"bgp:10.0.0.1:10.0.1.1:{HUB}:{PE}": IDLE, f"bgp:10.0.0.2:10.0.1.1:{HUB}:{PE}": UP}
    adjacency = {f"isis:{HUB}:Hu0/0/0/1:{PE}:Hu0/0/0/23": {"status": "up"}}
    unseen = {f"bgp:10.0.0.1:10.0.9.1:{HUB}:{MUTE}": {"local": "idle", "remote": "unknown", "status": "down"}}

    assert pi.cut_off_devices(_pe_case(), answered={HUB, FAR}) == [], "it did not answer"
    assert pi.cut_off_devices(_pe_case(issues=(failed,)), answered={PE, HUB, FAR}) == [], "its query failed"
    assert pi.cut_off_devices(_pe_case(bgp=one_up), answered={PE, HUB, FAR}) == [], "one session is up"
    assert pi.cut_off_devices(_pe_case(isis=adjacency), answered={PE, HUB, FAR}) == [], "an adjacency is up"
    assert pi.cut_off_devices(_pe_case(bgp=unseen), answered={HUB, FAR}) == [], "only the far end spoke"


@pytest.mark.asyncio
async def test_cut_off_device_is_investigated_once_and_explains_services_that_name_another_device():
    from diagnostic_mas.service_final_status import final_service_counts

    case, calls = _pe_case(), []

    explained = await _run(case, _loop(calls))

    assert calls == [f"device:{PE}"]
    assert explained == {"two-site", "external"}
    by_name = {d["subject"]["name"]: d for d in case.diagnoses}
    assert set(by_name) == {"two-site", "external"}
    dx = by_name["two-site"]
    assert (dx["source"], dx["status"], dx["complete"]) == ("device_investigation", "down", True)
    assert PE in dx["cause"] and FAR in dx["cause"] and "the only uplink is down" in dx["cause"]
    assert "not investigated individually" in dx["observed"]
    assert case.service_coverage["two-site"] == "device_investigated"
    assert final_service_counts(case)["l2sts"]["sources"]["down"] == {"Down — device investigated once": 1}
    # Config that is silent about other sites is left alone: that is the admins' rule to make.
    assert final_service_counts(case)["l3rt"]["up"] == 1
    assert (case.budget.port_investigations_used, case.budget.port_tools_used, case.budget.drills_used) == (1, 4, 0)


@pytest.mark.asyncio
async def test_device_investigation_without_a_conclusion_or_a_budget_explains_nothing():
    case = _pe_case()
    assert await _run(case, _loop([], conclude=False)) == set() and case.diagnoses == []

    case, calls = _pe_case(), []
    case.budget.max_port_investigations = 0
    assert await _run(case, _loop(calls)) == set() and calls == []

    case, calls = _pe_case(), []
    assert await _run(case, _loop(calls), answered=(HUB, FAR)) == set() and calls == []


@pytest.mark.asyncio
async def test_services_are_explained_only_when_the_investigation_itself_read_every_session_down():
    from diagnostic_mas.operator_report import format_devices_operator

    # A session came back between the collection and the investigation.
    case = _pe_case()
    assert await _run(case, _loop([], sessions="445667")) == set() and case.diagnoses == []
    block = "\n".join(format_devices_operator(case)).split(f"### {PE}\n", 1)[1].split("\n### ", 1)[0]
    assert ("**Cut off:** the only uplink is down (not confirmed when re-read; "
            "2 services left to their own investigation)") in block

    # The investigation never read the session states.
    case = _pe_case()
    assert await _run(case, _loop([], sessions=None)) == set() and case.diagnoses == []


@pytest.mark.asyncio
async def test_scan_investigates_the_device_first_and_only_the_remaining_services(monkeypatch):
    from diagnostic_mas import dataplane_verify

    case, devices, services = _pe_case(), [], []

    async def verify(client, settings, case, *, record, **kwargs):
        services.append(record["name"])
        return True

    monkeypatch.setattr("diagnostic_mas.drill.llm_drill_tool_loop", _loop(devices))
    monkeypatch.setattr("diagnostic_mas.port_investigation.successful_live_devices", lambda: [PE, HUB, FAR])
    monkeypatch.setattr(dataplane_verify, "llm_dataplane_verify_one", verify)

    await dataplane_verify.run_dataplane_verify_phase(
        None, SimpleNamespace(), case, device_names=set(case.device_names), max_per_category=30)

    assert devices == [f"device:{PE}"]
    assert sorted(services) == ["elsewhere", "local-unknown"]


@pytest.mark.asyncio
async def test_report_shows_the_device_finding_and_counts_it_with_the_port_investigations():
    from diagnostic_mas.html_report import _device_details
    from diagnostic_mas.operator_report import format_devices_operator, format_run_details
    from diagnostic_mas.report import format_dataplane_dig_line
    from diagnostic_mas.state_paths import case_to_dict

    case = _pe_case()
    await _run(case, _loop([]))

    block = "\n".join(format_devices_operator(case)).split(f"### {PE}\n", 1)[1].split("\n### ", 1)[0]

    assert "**Cut off:** the only uplink is down (2 services that use another device; investigated once)" in block
    assert "**Port and device investigations:** 1 / 6 (tools used 4); 2 services explained" in "\n".join(
        format_run_details(case))
    assert "2 services are explained by 1 device investigation" in format_dataplane_dig_line(case)
    details = _device_details(case_to_dict(case))[PE]
    assert "Cut-off investigation" in details and "uplink Hu0/0/0/23 down/down" in details
