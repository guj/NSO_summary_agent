"""Offline proofs for the port-mirror check and the interface-only check for unseen service types."""
import pytest

from diagnostic_mas.case import Budget, CaseFile, add_evidence
from diagnostic_mas.dataplane_verify import select_dataplane_candidates, typed_dataplane_prompt_stem
from diagnostic_mas.operational_checks.runner import evaluate
from diagnostic_mas.service_final_status import assessment
from nso_facts.service_collect import collect_service_health
from test_operational_service_types import call_from, record

def source(port, status="Operational", direction="Both"):
    """One source of a session, laid out as ``show monitor-session status detail`` prints it."""
    return (f"  {port}\n"
            f"    Direction:     {direction}\n"
            "    Port level:    True\n"
            "    ACL match:     Disabled\n"
            "    IPv4 ACL:      Disabled\n"
            "    IPv6 ACL:      Disabled\n"
            "    MPLS ACL:      Disabled\n"
            "    Portion:       Full packet\n"
            "    Interval:      Mirror all packets\n"
            "    Mirror drops:  Disabled\n"
            f"    Status:        {status}\n")


def session(name, destination, *sources):
    return (f"Monitor-session {name}\n  Destination interface {destination}\n"
            "  Source Interfaces\n  -----------------\n" + "".join(sources) + "\n")


STATUS = "monitor-session status detail"
SESSION = session("mon_pm1", "HundredGigE0/0/0/9", source("HundredGigE0/0/0/2"), source("HundredGigE0/0/0/3.100"))
PORTS = ("Hu0/0/0/9", "Hu0/0/0/2", "Hu0/0/0/3.100")

# What atla-data-sw returned on 13 October 2026, the first live sample: two services, one source each.
ATLA = """\r
\rTue Oct 13 04:15:27.810 UTC\r
Monitor-session mon_ATLA_Patchwork_A-7af33e30-a377-4605-bb3d-243b49e72e39\r
  Destination interface TwentyFiveGigE0/0/0/12/1\r
  Source Interfaces\r
  -----------------\r
  HundredGigE0/0/0/22\r
    Direction:     Both\r
    Port level:    True\r
    ACL match:     Disabled\r
    IPv4 ACL:      Disabled\r
    IPv6 ACL:      Disabled\r
    MPLS ACL:      Disabled\r
    Portion:       Full packet\r
    Interval:      Mirror all packets\r
    Mirror drops:  Disabled\r
    Status:        Not operational (source state down). \r
\r
Monitor-session mon_ATLA_Patchwork_A-f655fd58-132e-4094-9006-66c16d6dfca9\r
  Destination interface TwentyFiveGigE0/0/0/12/0\r
  Source Interfaces\r
  -----------------\r
  HundredGigE0/0/0/0\r
    Direction:     Both\r
    Port level:    True\r
    ACL match:     Disabled\r
    IPv4 ACL:      Disabled\r
    IPv6 ACL:      Disabled\r
    MPLS ACL:      Disabled\r
    Portion:       Full packet\r
    Interval:      Mirror all packets\r
    Mirror drops:  Disabled\r
    Status:        Operational\r
RP/0/RP0/CPU0:atla-data-sw#"""


def mirror(**changes):
    """A port-mirror service on device ``a``: two sources mirrored to Hu0/0/0/9."""
    return {
        "name": "pm1", "device": "a",
        "from-interface": [{"type": "HundredGigE", "id": "0/0/0/2"}],
        "from-interface-vlan": [{"type": "HundredGigE", "id": "0/0/0/3", "outervlan": 100}],
        "to-interface": {"type": "HundredGigE", "id": "0/0/0/9"},
        **changes,
    }


def device_answers(answer=SESSION, down=()):
    answers = {("a", STATUS): answer}
    for port in PORTS:
        state = "down, line protocol is down" if port in down else "up, line protocol is up"
        answers[("a", f"interfaces {port}")] = f"{port} is {state}"
    return answers


async def check_mirror(answers, instance=None, calls=None, cache=None):
    return await evaluate(record("port-mirror", ["a"]), instance or mirror(), None,
                          call_from(answers, [] if calls is None else calls),
                          {} if cache is None else cache)


@pytest.mark.asyncio
async def test_port_mirror_is_up_when_its_session_sources_and_ports_are_all_up():
    result = await check_mirror(device_answers())

    assert result["status"] == "up"
    assert not result["customer_delivery_tested"]


@pytest.mark.asyncio
async def test_port_mirror_is_down_when_a_source_is_not_operational():
    # The second status is the one amst-data-sw gave on 13 October 2026 for a source that another session mirrors to.
    for status in ("Not operational (source state down).", "Not operational (source interface is also a destination)."):
        answer = session("mon_pm1", "HundredGigE0/0/0/9", source("HundredGigE0/0/0/2", status),
                         source("HundredGigE0/0/0/3.100"))

        result = await check_mirror(device_answers(answer))

        assert result["status"] == "down"
        assert f"Hu0/0/0/2: {status}" in result["reason"]


# The reasons devices gave for the 34 port-mirror services found Down on 13 October 2026.
CONFLICTS = ("Not operational (source interface is also a destination).",
             "Not operational (source same as destination).",
             "Not operational (destination not active).")


@pytest.mark.parametrize("status", CONFLICTS)
@pytest.mark.asyncio
async def test_source_the_device_calls_not_operational_is_down_with_no_investigation(status):
    answer = session("mon_pm1", "HundredGigE0/0/0/9", source("HundredGigE0/0/0/2", status),
                     source("HundredGigE0/0/0/3.100"))

    result = await check_mirror(device_answers(answer))

    assert (result["status"], result["needs_investigation"]) == ("down", False)
    assert [c["observation"] for c in result["checks"] if c.get("conclusive")] == [f"Hu0/0/0/2: {status}"]


@pytest.mark.asyncio
async def test_source_that_is_down_itself_still_gets_its_port_looked_at():
    answer = session("mon_pm1", "HundredGigE0/0/0/9",
                     source("HundredGigE0/0/0/2", "Not operational (source state down)."), source("HundredGigE0/0/0/3.100"))

    result = await check_mirror(device_answers(answer, down=("Hu0/0/0/2",)))

    # The device's reason is recorded, and the dark port keeps its investigation.
    assert (result["status"], result["needs_investigation"]) == ("down", True)


@pytest.mark.asyncio
async def test_status_the_check_does_not_know_is_a_fault_that_is_still_investigated():
    answer = session("mon_pm1", "HundredGigE0/0/0/9", source("HundredGigE0/0/0/2", "Initializing"),
                     source("HundredGigE0/0/0/3.100"))

    result = await check_mirror(device_answers(answer))

    assert (result["status"], result["needs_investigation"]) == ("down", True)


def test_scan_reports_such_a_service_down_without_sending_it_to_the_llm():
    from diagnostic_mas.dataplane_verify import init_service_coverage
    from diagnostic_mas.drill import select_drill_issues
    from diagnostic_mas.case import open_issue
    from diagnostic_mas.report import render_report
    from diagnostic_mas.service_final_status import final_service_assessments

    reason = f"Hu0/0/0/2: {CONFLICTS[0]}"
    rec = record("port-mirror", ["a"]) | {
        "name": "pm1", "status": "down", "operational_status": "down", "dataplane_status": "not_checked",
        "basic_checks": {"schema_version": 1, "status": "down", "reason": reason, "needs_investigation": False,
                         "sync_ready": True, "checks": [{"check": "source", "status": "fault", "device": "a",
                                                         "observation": reason, "conclusive": True}]}}
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = ["a"]
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services",
                        "payload": {"extra": {"services": {"port-mirror/pm1": rec}}}})
    open_issue(case, code="service_down", severity="high", layer="services", edge_id="pm1", devices=["a"],
               message="port-mirror pm1: down", evidence_ids=[])
    init_service_coverage(case)

    assert case.service_coverage["pm1"] == "fixed_check_decided"
    assert "pm1" not in {r["name"] for _ev, r in select_dataplane_candidates(case)}
    assert select_drill_issues(case, limit=2) == []
    assert {name: (status, why) for _k, name, _s, status, why in final_service_assessments(case)}["pm1"] == (
        "down", "Down — decided by the fixed check")
    report = render_report(case, summary="s", duration_seconds=1, dry_run=True, llm_model="m")
    assert "**Coverage:** Decided by the fixed check (no investigation needed)" in report
    assert CONFLICTS[0] in report


@pytest.mark.asyncio
async def test_port_mirror_reads_the_layout_a_live_device_returned():
    def atla(name, source_port, destination):
        return {"name": name, "device": "atla-data-sw", "direction": "both",
                "from-interface": [{"type": "HundredGigE", "id": source_port}],
                "to-interface": {"type": "TwentyFiveGigE", "id": destination}}

    answers = {("atla-data-sw", STATUS): ATLA}
    for port, state in (("TF0/0/0/12/1", "up"), ("Hu0/0/0/22", "down"), ("TF0/0/0/12/0", "up"), ("Hu0/0/0/0", "up")):
        answers["atla-data-sw", f"interfaces {port}"] = f"{port} is {state}, line protocol is {state}"

    dark = await evaluate(record("port-mirror", ["atla-data-sw"]), atla("dark", "0/0/0/22", "0/0/0/12/1"), None,
                          call_from(answers, []), {})
    fine = await evaluate(record("port-mirror", ["atla-data-sw"]), atla("fine", "0/0/0/0", "0/0/0/12/0"), None,
                          call_from(answers, []), {})

    assert dark["status"] == "down"
    assert "Hu0/0/0/22: Not operational (source state down)." in dark["reason"]
    assert (fine["status"], fine["needs_investigation"]) == ("up", False)
    assert ("source", "pass", "Hu0/0/0/0 both: Operational") in {
        (c["check"], c["status"], c["observation"]) for c in fine["checks"]}


@pytest.mark.asyncio
async def test_port_mirror_is_down_when_its_destination_port_is_down():
    result = await check_mirror(device_answers(down=("Hu0/0/0/9",)))

    assert result["status"] == "down"


@pytest.mark.asyncio
async def test_port_mirror_session_is_matched_by_destination_not_by_a_built_name():
    renamed = SESSION.replace("mon_pm1", "some-other-name")

    result = await check_mirror(device_answers(renamed))

    assert result["status"] == "up"
    assert any(c["check"] == "session" and "some-other-name" in c["observation"]
               for c in result["checks"])


@pytest.mark.parametrize("answer", [
    "",                                                        # the device listed no session
    SESSION.replace("HundredGigE0/0/0/9", "HundredGigE0/0/0/7"),  # only another service's session
    "Some layout this parser does not know\n",
])
@pytest.mark.asyncio
async def test_port_mirror_is_unknown_not_down_when_its_session_is_not_in_the_answer(answer):
    result = await check_mirror(device_answers(answer))

    assert result["status"] == "unknown"


@pytest.mark.asyncio
async def test_port_mirror_is_unknown_when_a_source_is_missing_or_mirrored_in_another_direction():
    missing = session("mon_pm1", "HundredGigE0/0/0/9", source("HundredGigE0/0/0/2"))

    assert (await check_mirror(device_answers(missing)))["status"] == "unknown"
    assert (await check_mirror(device_answers(), mirror(direction="rx-only")))["status"] == "unknown"


@pytest.mark.asyncio
async def test_services_on_one_device_share_a_single_status_command():
    calls, cache = [], {}

    await check_mirror(device_answers(), calls=calls, cache=cache)
    await check_mirror(device_answers(), mirror(name="pm2"), calls=calls, cache=cache)

    assert calls.count(("a", STATUS)) == 1


# --- service types the agent has no module for ----------------------------------

def unseen(*ports):
    """An instance of a service type nobody has written checks for."""
    return {"name": "x1", "device": "a",
            "uplink": [{"type": "HundredGigE", "id": port} for port in ports]}


def port_answers(down=()):
    return {("a", f"interfaces Hu{port}"):
            f"Hu{port} is " + ("down, line protocol is down" if port in down else "up, line protocol is up")
            for port in ("0/0/0/4", "0/0/0/5")}


async def check_unseen(answers, instance):
    return await evaluate(record("new-kind", ["a"]), instance, None, call_from(answers, []), {})


@pytest.mark.asyncio
async def test_unseen_type_with_every_port_up_stays_unknown_and_says_why():
    result = await check_unseen(port_answers(), unseen("0/0/0/4", "0/0/0/5"))

    assert result["status"] == "unknown"
    assert "only its interfaces were examined" in result["reason"]
    assert assessment({**record("new-kind", ["a"]), "basic_checks": result}) == (
        "unknown", "Interfaces up — service logic not checked")


@pytest.mark.asyncio
async def test_unseen_type_is_down_when_one_of_its_ports_is_down():
    result = await check_unseen(port_answers(down=("0/0/0/5",)), unseen("0/0/0/4", "0/0/0/5"))

    assert result["status"] == "down"
    assert any(c["status"] == "fault" and c["observation"] == "Hu0/0/0/5" for c in result["checks"])


@pytest.mark.asyncio
async def test_unseen_type_without_an_identifiable_port_is_unknown():
    result = await check_unseen({}, {"name": "x1", "device": "a"})

    assert result["status"] == "unknown"
    assert "No interface could be identified" in result["reason"]


@pytest.mark.asyncio
async def test_a_scan_gives_an_unseen_type_its_interface_check(monkeypatch):
    import nso_facts.service_collect as sc

    monkeypatch.setattr(sc, "call_mcp", call_from(port_answers(), []))
    listed = {"new-kind": {"status": "success", "data": {"services": [unseen("0/0/0/4")]}}}

    services = await collect_service_health(None, listed, {}, {"a": "in-sync"},
                                            service_sync_mode="skip", operational_policy="routine")

    assert services["new-kind/x1"]["basic_checks"]["status"] == "unknown"
    assert services["new-kind/x1"]["operational_status"] == "unknown"


# --- which of them the LLM may investigate ---------------------------------------

def test_port_mirror_has_its_own_prompt_and_unseen_types_have_none():
    assert typed_dataplane_prompt_stem("port-mirror") == "port-mirror"
    assert typed_dataplane_prompt_stem("new-kind") is None


def test_llm_investigates_a_failing_port_mirror_but_no_unseen_type():
    def failing(kind):
        return {"name": kind, "service_type": kind, "devices": ["a"], "system_status": "up",
                "dataplane_status": "not_checked", "status": "unknown",
                "basic_checks": {"status": "unknown", "needs_investigation": True, "checks": []}}

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services", "payload": {
        "extra": {"services": {f"{k}/{k}": failing(k) for k in ("port-mirror", "new-kind")}}}})

    picked = select_dataplane_candidates(case, per_category=30)

    assert [r["service_type"] for _evidence, r in picked] == ["port-mirror"]
