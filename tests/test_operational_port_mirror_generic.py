"""Offline proofs for the port-mirror check and the interface-only check for unseen service types."""
import pytest

from diagnostic_mas.case import Budget, CaseFile, add_evidence
from diagnostic_mas.dataplane_verify import select_dataplane_candidates, typed_dataplane_prompt_stem
from diagnostic_mas.operational_checks.runner import evaluate
from diagnostic_mas.service_final_status import assessment
from nso_facts.service_collect import collect_service_health
from test_operational_service_types import call_from, record

# The status layout Cisco documents for IOS-XR; not yet seen from a live device here.
SESSION = (
    "Monitor-session mon_pm1\n"
    "Destination interface HundredGigE0/0/0/9\n"
    "================================================================================\n"
    "Source Interface      Dir   Status\n"
    "--------------------- ----  ----------------------------------------------------\n"
    "Hu0/0/0/2             Both  Operational\n"
    "Hu0/0/0/3.100         Both  Operational\n"
)
PORTS = ("Hu0/0/0/9", "Hu0/0/0/2", "Hu0/0/0/3.100")


def mirror(**changes):
    """A port-mirror service on device ``a``: two sources mirrored to Hu0/0/0/9."""
    return {
        "name": "pm1", "device": "a",
        "from-interface": [{"type": "HundredGigE", "id": "0/0/0/2"}],
        "from-interface-vlan": [{"type": "HundredGigE", "id": "0/0/0/3", "outervlan": 100}],
        "to-interface": {"type": "HundredGigE", "id": "0/0/0/9"},
        **changes,
    }


def device_answers(session=SESSION, down=()):
    answers = {("a", "monitor-session status"): session}
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
    session = SESSION.replace(
        "Hu0/0/0/2             Both  Operational",
        "Hu0/0/0/2             Both  Not operational (destination interface down)")

    result = await check_mirror(device_answers(session))

    assert result["status"] == "down"
    assert "Not operational (destination interface down)" in result["reason"]


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
    missing = SESSION.replace("Hu0/0/0/3.100         Both  Operational\n", "")

    assert (await check_mirror(device_answers(missing)))["status"] == "unknown"
    assert (await check_mirror(device_answers(), mirror(direction="rx-only")))["status"] == "unknown"


@pytest.mark.asyncio
async def test_services_on_one_device_share_a_single_status_command():
    calls, cache = [], {}

    await check_mirror(device_answers(), calls=calls, cache=cache)
    await check_mirror(device_answers(), mirror(name="pm2"), calls=calls, cache=cache)

    assert calls.count(("a", "monitor-session status")) == 1


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
