"""A down port that Down services attach to is investigated once, before the services."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from diagnostic_mas import port_investigation as pi
from diagnostic_mas.case import Budget, CaseFile, add_evidence, debit_drill, open_issue
from diagnostic_mas.drill import record_drill_finding

MICH, FIU = "mich-data-sw", "fiu-data-sw"


def _service(name: str, kind: str, device: str, checks: list[tuple[str, str, str]]) -> dict:
    status = "down" if any(s == "fault" for _c, s, _o in checks) else "up"
    return {
        "name": name, "service_type": kind, "devices": [device],
        "in_sync": True, "device_sync": {device: "in-sync"},
        "system_status": "up", "dataplane_status": "not_checked", "status": status,
        "basic_checks": {
            "status": status, "needs_investigation": status != "up",
            "checks": [{"check": c, "status": s, "device": device, "observation": o}
                       for c, s, o in checks],
        },
    }


def _on_dark_lane(name: str, lane: str, kind: str = "l2bridge", device: str = MICH) -> dict:
    """A two-attachment service whose second attachment sits on a dark lane."""
    return _service(name, kind, device, [
        ("attachment", "pass", "Hu0/0/0/13.2055"),
        ("attachment", "fault", f"{lane}.0"),
        ("membership", "fault", f"{lane}.0 in bg-{name}:bd-{name}"),
        ("local_forwarding", "unknown", f"Programmed bridge member {lane}.0"),
    ])


INTERFACES = {
    (MICH, "HundredGigE0/0/0/13"): "up",
    (MICH, "HundredGigE0/0/0/24/0"): "down", (MICH, "HundredGigE0/0/0/24/0.0"): "down",
    (MICH, "HundredGigE0/0/0/24/1"): "down",
    (MICH, "HundredGigE0/0/0/25/0"): "down",
    (MICH, "HundredGigE0/0/0/30"): "admin-down",
    (FIU, "HundredGigE0/0/0/24/0"): "down",
}


def _case(*services: dict) -> CaseFile:
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = [MICH, FIU]
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services", "payload": {"extra": {
        "services": {f"{s['service_type']}/{s['name']}": s for s in services},
        "physical_operational_edges": [
            {"id": f"if:{device}:{name}", "state": {"admin": state, "oper": state}}
            for (device, name), state in INTERFACES.items()],
    }}})
    for s in services:
        if s["status"] == "down":
            open_issue(case, code="service_down", message=f"{s['name']} down", evidence_ids=[],
                       layer="services", edge_id=s["name"], devices=list(s["devices"]))
    return case


def _mich_case() -> CaseFile:
    return _case(
        _on_dark_lane("a", "Hu0/0/0/24/0"),
        _on_dark_lane("b", "Hu0/0/0/24/1"),
        _on_dark_lane("c", "Hu0/0/0/25/0"),
        # Also has a fault that is not on a link: its own investigation is still needed.
        _service("d", "l2sts", FIU, [("attachment", "fault", "Hu0/0/0/24/0.0"),
                                     ("replication", "fault", "no remote peer")]),
        _service("e", "l2bridge", MICH, [("attachment", "pass", "Hu0/0/0/13.2055")]),
        # Shut by configuration: the reason is already known, nothing to investigate.
        _service("f", "l2bridge", MICH, [("attachment", "fault", "Hu0/0/0/30.5")]),
    )


def _read_links(case, focus_issue, session, *, up=(), unread=()):
    """Record a `show interface` answer for each link the Issue lists."""
    for link in re.findall(r"link (\S+) recorded down", focus_issue["message"]):
        if link in unread:
            continue
        state = "up, line protocol is up" if link in up else "down, line protocol is down"
        add_evidence(case, {"kind": "drill", "role": "drill", "layer": "services", "payload": {
            "check": "exec_show", "issue_edge_id": session.issue_edge_id,
            "args": {"device_name": focus_issue["devices"][0], "input_command": f"interface {link}"},
            "result": {"result": f"{link} is {state}\n  Hardware is HundredGigE"}}})


def _loop(calls: list[str], *, conclude: bool = True, up=(), unread=()):
    async def fake(client, settings, case, *, device_names, focus_issue, session,
                   openai_client=None, system_prompt=None, account="drill"):
        calls.append(focus_issue["edge_id"])
        assert system_prompt and account == "port"
        debit_drill(case, 3, session=session, account="port")
        _read_links(case, focus_issue, session, up=up, unread=unread)
        if conclude:
            record_drill_finding(case, {
                "observed": "each listed lane: down, no receive light", "cause": "no light arriving",
                "fix_suggestion": "check the fibre", "fix_requires_human_approval": True,
                "confidence": "high"}, session=session)
        return 3, conclude
    return fake


async def _run(case: CaseFile, loop) -> set[str]:
    return await pi.run_port_investigations(
        None, SimpleNamespace(), case, device_names=set(case.device_names), tool_loop=loop)


def test_port_of_folds_sub_interfaces_and_breakout_lanes():
    assert pi.port_of("Hu0/0/0/24/1.0") == "Hu0/0/0/24"
    assert pi.port_of("HundredGigE0/0/0/13.2055") == "HundredGigE0/0/0/13"
    assert pi.port_of("Bundle-Ether100.3000") == "Bundle-Ether100"
    assert pi.port_of("HundredGigE0/0/0/24") == "HundredGigE0/0/0/24"


def test_targets_are_down_ports_whose_services_they_fully_explain():
    assert pi.port_targets(_mich_case()) == [
        {"device": MICH, "port": "HundredGigE0/0/0/24",
         "links": {"HundredGigE0/0/0/24/0": ["a"], "HundredGigE0/0/0/24/1": ["b"]}},
        {"device": MICH, "port": "HundredGigE0/0/0/25", "links": {"HundredGigE0/0/0/25/0": ["c"]}},
    ]


@pytest.mark.asyncio
async def test_each_port_is_investigated_once_and_its_services_are_explained():
    from diagnostic_mas.service_final_status import final_service_counts

    case, calls = _mich_case(), []

    explained = await _run(case, _loop(calls))

    assert calls == [f"if:{MICH}:HundredGigE0/0/0/24", f"if:{MICH}:HundredGigE0/0/0/25"]
    assert explained == {"a", "b", "c"}
    by_name = {d["subject"]["name"]: d for d in case.diagnoses}
    assert set(by_name) == {"a", "b", "c"}
    assert (by_name["a"]["source"], by_name["a"]["status"], by_name["a"]["complete"]) == (
        "port_investigation", "down", True)
    assert "HundredGigE0/0/0/24" in by_name["a"]["cause"] and "no light arriving" in by_name["a"]["cause"]
    assert "not investigated individually" in by_name["a"]["observed"]
    assert case.service_coverage["a"] == "port_investigated"
    assert {i["edge_id"]: i["status"] for i in case.issues if i["layer"] == "services"}["a"] == "explained"
    assert final_service_counts(case)["l2bridge"]["sources"]["down"]["Down — port investigated once"] == 3
    assert (case.budget.port_investigations_used, case.budget.port_tools_used) == (2, 6)
    assert case.budget.drills_used == 0


@pytest.mark.asyncio
async def test_ceiling_leaves_the_remaining_ports_to_the_service_investigations():
    case, calls = _mich_case(), []
    case.budget.max_port_investigations = 1

    assert await _run(case, _loop(calls)) == {"a", "b"}
    assert len(calls) == 1

    case, calls = _mich_case(), []
    case.budget.max_port_investigations = 0

    assert await _run(case, _loop(calls)) == set() and calls == []


@pytest.mark.asyncio
async def test_port_investigation_without_a_conclusion_explains_nothing():
    case, calls = _mich_case(), []

    assert await _run(case, _loop(calls, conclude=False)) == set()
    assert case.diagnoses == []
    assert case.service_coverage.get("a") != "port_investigated"


@pytest.mark.asyncio
async def test_service_on_two_ports_is_explained_only_when_both_were_investigated():
    both = _service("g", "l2bridge", MICH, [("attachment", "fault", "Hu0/0/0/24/0.0"),
                                           ("attachment", "fault", "Hu0/0/0/25/0.0")])
    case = _case(both, _on_dark_lane("a", "Hu0/0/0/24/1"))
    case.budget.max_port_investigations = 1

    assert "g" not in await _run(case, _loop([]))

    case = _case(both, _on_dark_lane("a", "Hu0/0/0/24/1"))

    assert await _run(case, _loop([])) == {"a", "g"}


@pytest.mark.asyncio
async def test_explained_services_are_not_sent_to_the_service_investigation():
    from diagnostic_mas.dataplane_verify import select_dataplane_candidates

    case = _mich_case()
    explained = await _run(case, _loop([]))

    picked = select_dataplane_candidates(case, per_category=30, exclude=explained)

    assert sorted(rec["name"] for _ev, rec in picked) == ["d", "f"]


@pytest.mark.asyncio
async def test_report_shows_the_port_finding_and_counts_it_apart_from_service_investigations():
    from diagnostic_mas.operator_report import format_devices_operator, format_run_details
    from diagnostic_mas.report import format_dataplane_dig_line

    case = _mich_case()
    await _run(case, _loop([]))

    devices = "\n".join(format_devices_operator(case))
    block = devices.split(f"### {MICH}\n", 1)[1].split("\n### ", 1)[0]

    assert "**Port HundredGigE0/0/0/24:** no light arriving (2 services" in block
    assert "**Port HundredGigE0/0/0/25:** no light arriving (1 service" in block
    assert "**Port and device investigations:** 2 / 6 (tools used 6); 3 services explained" in "\n".join(
        format_run_details(case))
    line = format_dataplane_dig_line(case)
    assert "3 services are explained by 2 port investigations" in line
    assert "received additional dataplane investigation" not in line or "Only 3" not in line


@pytest.mark.asyncio
async def test_explained_fault_is_carried_in_the_fault_history_like_any_other():
    from diagnostic_mas.case_delta import service_fault_history
    from diagnostic_mas.state_paths import case_to_dict

    case = _mich_case()
    await _run(case, _loop([]))

    history = {row["service"]: row for row in service_fault_history({}, case_to_dict(case), run_id="r1")}

    assert history["a"]["status"] == "down" and history["a"]["source"] == "port_investigation"


class _FakeLLM:
    """Asks for one reading, then concludes; records the prompt it was given."""

    def __init__(self):
        self.system_prompts: list[str] = []
        outer = self

        class _Completions:
            @staticmethod
            def create(**kwargs):
                messages = kwargs["messages"]
                outer.system_prompts.append(messages[0]["content"])
                first_turn = not any(m.get("role") == "tool" for m in messages)
                name, args = ("mcp_call", {"tool_name": "exec_show", "params": {
                    "device_name": MICH, "input_command": "interface HundredGigE0/0/0/24/0"}}
                ) if first_turn else ("conclude_investigation", {
                    "observed": "24/0: Rx -40 dBm", "cause": "no light arriving", "confidence": "high"})
                call = SimpleNamespace(id="c1", function=SimpleNamespace(name=name, arguments=json.dumps(args)))
                message = SimpleNamespace(content=None, tool_calls=[call])
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        self.chat = SimpleNamespace(completions=_Completions)


@pytest.mark.asyncio
async def test_real_loop_uses_the_port_prompt_and_its_own_tool_count(monkeypatch):
    async def fake_device(client, plan):
        return [{"check": "exec_show", "result": "HundredGigE0/0/0/24/0 is down, line protocol is down"}]

    monkeypatch.setattr("diagnostic_mas.drill.run_deep_checks", fake_device)
    case, llm = _case(_on_dark_lane("a", "Hu0/0/0/24/0")), _FakeLLM()
    settings = SimpleNamespace(fabric_api_key="k", fabric_model="m", fabric_temperature=None)

    explained = await pi.run_port_investigations(
        None, settings, case, device_names=set(case.device_names), openai_client=llm)

    assert explained == {"a"}
    assert all("ONE down physical port" in prompt for prompt in llm.system_prompts)
    assert (case.budget.port_tools_used, case.budget.drills_used, case.budget.drill_issues_used) == (1, 0, 0)
    port_issue = next(i for i in case.issues if i["code"] == "port_down")
    assert port_issue["status"] == "explained" and "HundredGigE0/0/0/24/0" in port_issue["message"]


@pytest.mark.asyncio
async def test_scan_investigates_ports_first_and_only_the_remaining_services(monkeypatch):
    from diagnostic_mas import dataplane_verify

    case, ports, services = _mich_case(), [], []

    async def verify(client, settings, case, *, record, **kwargs):
        services.append(record["name"])
        return True

    monkeypatch.setattr("diagnostic_mas.drill.llm_drill_tool_loop", _loop(ports))
    monkeypatch.setattr(dataplane_verify, "llm_dataplane_verify_one", verify)

    await dataplane_verify.run_dataplane_verify_phase(
        None, SimpleNamespace(), case, device_names=set(case.device_names), max_per_category=30)

    assert len(ports) == 2 and sorted(services) == ["d", "f"]


@pytest.mark.asyncio
async def test_scan_falls_back_to_service_investigations_when_the_port_step_fails(monkeypatch):
    from diagnostic_mas import dataplane_verify

    case, services = _mich_case(), []

    async def broken(*args, **kwargs):
        raise RuntimeError("port step broke")

    async def verify(client, settings, case, *, record, **kwargs):
        services.append(record["name"])
        return True

    monkeypatch.setattr("diagnostic_mas.drill.llm_drill_tool_loop", broken)
    monkeypatch.setattr(dataplane_verify, "llm_dataplane_verify_one", verify)

    await dataplane_verify.run_dataplane_verify_phase(
        None, SimpleNamespace(), case, device_names=set(case.device_names), max_per_category=30)

    assert sorted(services) == ["a", "b", "c", "d", "f"]


@pytest.mark.asyncio
async def test_run_details_do_not_count_explained_services_as_investigations():
    from diagnostic_mas.operator_report import format_run_details

    case = _mich_case()
    await _run(case, _loop([]))

    assert not any("Dataplane dig tools" in line and "3 dig" in line for line in format_run_details(case))


LONG_CAUSE = "No light arrives on any lane. " + "More detail about each lane follows here. " * 30
LONG_OBSERVED = "LANE-READINGS " + "lane reading; " * 80


def _wordy_loop():
    async def fake(client, settings, case, *, device_names, focus_issue, session,
                   openai_client=None, system_prompt=None, account="drill"):
        _read_links(case, focus_issue, session)
        record_drill_finding(case, {
            "observed": LONG_OBSERVED, "cause": LONG_CAUSE, "fix_suggestion": "check the fibre",
            "fix_requires_human_approval": True, "confidence": "high"}, session=session)
        return 1, True
    return fake


@pytest.mark.asyncio
async def test_long_port_finding_is_short_in_the_text_report_and_whole_in_the_html_details():
    from diagnostic_mas.html_report import _device_details
    from diagnostic_mas.operator_report import format_devices_operator
    from diagnostic_mas.state_paths import case_to_dict

    case = _mich_case()
    await _run(case, _wordy_loop())

    port_lines = [line for line in format_devices_operator(case) if line.startswith("**Port ")]
    by_name = {d["subject"]["name"]: d for d in case.diagnoses}

    assert port_lines and all(len(line) < 420 for line in port_lines)
    assert all(line.startswith("**Port HundredGigE0/0/0/2") and "No light arrives on any lane." in line
               for line in port_lines)
    # Each explained service points at the port; the port's readings are not repeated in it.
    assert "LANE-READINGS" not in by_name["a"]["observed"]
    assert "not investigated individually" in by_name["a"]["observed"]
    assert len(by_name["a"]["cause"]) < 420
    details = _device_details(case_to_dict(case))[MICH]
    assert "Port HundredGigE0/0/0/24" in details and "LANE-READINGS" in details
    assert LONG_CAUSE.strip()[-40:] in details


@pytest.mark.asyncio
async def test_service_is_explained_only_when_the_investigation_itself_read_its_link_down():
    from diagnostic_mas.dataplane_verify import select_dataplane_candidates
    from diagnostic_mas.operator_report import format_devices_operator

    # Lane 24/1 came back between the collection and the investigation; lane 25/0 was never read.
    case = _mich_case()
    explained = await _run(case, _loop([], up={"HundredGigE0/0/0/24/1"}, unread={"HundredGigE0/0/0/25/0"}))

    assert explained == {"a"}
    assert {d["subject"]["name"] for d in case.diagnoses} == {"a"}
    still_investigated = {rec["name"] for _ev, rec in select_dataplane_candidates(
        case, per_category=30, exclude=explained)}
    assert {"b", "c"} <= still_investigated
    block = "\n".join(format_devices_operator(case)).split(f"### {MICH}\n", 1)[1].split("\n### ", 1)[0]
    assert ("**Port HundredGigE0/0/0/24:** no light arriving (1 service on a link read down; "
            "1 left to its own investigation: its link was not read down)") in block
    assert ("**Port HundredGigE0/0/0/25:** no light arriving (no link read down; "
            "1 service left to its own investigation)") in block


def test_cli_ceiling_for_port_investigations():
    from diagnostic_mas.run import build_parser

    parser = build_parser()

    assert parser.parse_args([]).max_port_investigations == 6
    assert parser.parse_args(["--max-port-investigations", "0"]).max_port_investigations == 0
