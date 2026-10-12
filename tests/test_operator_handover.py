"""What the report hands to the person who acts on it: a dark port's details, and a delivery check to run."""
from diagnostic_mas import port_investigation as pi
from diagnostic_mas.case import Budget, CaseFile, add_evidence
from diagnostic_mas.html_report import _device_details
from diagnostic_mas.report import render_report
from diagnostic_mas.state_paths import case_to_dict

MICH, PORT = "mich-data-sw", "HundredGigE0/0/0/24"


def _interface(name: str, *, description: str | None = None) -> str:
    return (f"\n{name} is down, line protocol is down \n  Interface state transitions: 118\n"
            + (f"  Description: {description}\n" if description else "")
            + "  Hardware is HundredGigE, address is 7cf8.804e.8a64\n  Last link flapped 8w1d\n"
            "RP/0/RP0/CPU0:mich-data-sw#")


OPTICS = "\n Lane  Laser Bias    TX Power    RX Power\n  1    7.9 mA   2.24 dBm   -40.00 dBm\nRP/0/RP0/CPU0:mich-data-sw#"
READINGS = [
    (f"interface {PORT}/0", _interface(f"{PORT}/0", description="to umich panel 12, port 3")),
    (f"interface {PORT}/1", _interface(f"{PORT}/1")),
    ("controllers optics 0/0/0/24", OPTICS),
    ("", "a tool answer that carries no device command"),
]
LINKS = [f"{PORT}/0", f"{PORT}/1", f"{PORT}/2"]


def test_port_investigation_keeps_each_links_description_and_the_commands_behind_its_readings():
    assert pi._link_descriptions(LINKS, READINGS) == {f"{PORT}/0": "to umich panel 12, port 3", f"{PORT}/1": ""}
    assert pi._reading_commands(LINKS, READINGS) == {
        "light": ["controllers optics 0/0/0/24"],
        "state": [f"interface {PORT}/0", f"interface {PORT}/1"]}


def _service(name: str, kind: str, devices: list[str], intent: dict) -> dict:
    return {"name": name, "service_type": kind, "devices": devices, "in_sync": True, "system_status": "up",
            "device_sync": {d: "in-sync" for d in devices}, "status": "down",
            "basic_checks": {"status": "down", "needs_investigation": True, "checks": [], "intent": intent}}


def _lane(n: int) -> dict:
    return {"type": "HundredGigE", "id": f"0/0/0/24/{n}", "outervlan": 0}


def test_device_block_gives_the_technician_what_to_look_at_and_how_it_was_read():
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = [MICH, "rutg-data-sw"]
    local = _service("ms-00fc7768", "l2bridge", [MICH], {"device": MICH, "interface": [
        {"type": "HundredGigE", "id": "0/0/0/13", "outervlan": 2109}, _lane(0)]})
    remote = _service("net1-01be4f9e", "l2sts", [MICH, "rutg-data-sw"], {
        "site-a": {"device": MICH, "interface": [_lane(1)]},
        "site-z": {"device": "rutg-data-sw", "interface": [{"type": "HundredGigE", "id": "0/0/0/23", "outervlan": 100}]}})
    alone = _service("solo", "l2bridge", [MICH], {"device": MICH, "interface": [_lane(2)]})
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services", "payload": {"extra": {
        "services": {f"{s['service_type']}/{s['name']}": s for s in (local, remote, alone)}}}})
    add_evidence(case, {"kind": pi.SOURCE, "role": "port", "layer": "physical", "payload": {
        "device": MICH, "port": PORT, "concluded": True, "observed": "no light", "cause": "dark", "fix_suggestion": "clean",
        "links": {f"{PORT}/0": ["ms-00fc7768"], f"{PORT}/1": ["net1-01be4f9e"], f"{PORT}/2": ["solo"]},
        "link_states": {link: "down" for link in LINKS},
        "link_ages": {f"{PORT}/0": "8w1d", f"{PORT}/1": "never"},
        "link_descriptions": pi._link_descriptions(LINKS, READINGS),
        "commands": pi._reading_commands(LINKS, READINGS)}})

    block = _device_details(case_to_dict(case))[MICH]

    assert """  For the technician:
    HundredGigE0/0/0/24/0: description &quot;to umich panel 12, port 3&quot;; down 8w1d
      service ms-00fc7768: other attachment Hu0/0/0/13.2109 on mich-data-sw
    HundredGigE0/0/0/24/1: no description on the device; never came up
      service net1-01be4f9e: other attachment Hu0/0/0/23.100 on rutg-data-sw
    HundredGigE0/0/0/24/2: description not read
      service solo: no other attachment in its config
    Light levels from: show controllers optics 0/0/0/24
    Link state from: show interface HundredGigE0/0/0/24/0, show interface HundredGigE0/0/0/24/1""" in block


def test_older_port_finding_without_these_fields_is_shown_as_before():
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = [MICH]
    add_evidence(case, {"kind": pi.SOURCE, "role": "port", "layer": "physical", "payload": {
        "device": MICH, "port": PORT, "concluded": True, "observed": "o", "cause": "c", "fix_suggestion": None,
        "links": {}, "link_states": {}}})

    block = _device_details(case_to_dict(case))[MICH]

    assert "Port HundredGigE0/0/0/24 investigation" in block and "For the technician" not in block


# --- a delivery check for an operator, on a clear l3rt ------------------------------

def _l3rt(name: str, intent: dict, *, basic: str = "unknown", kind: str = "l3rt") -> dict:
    return {"name": name, "service_type": kind, "devices": ["ncsa-data-sw"], "in_sync": True, "system_status": "up",
            "device_sync": {"ncsa-data-sw": "in-sync"}, "status": basic,
            "basic_checks": {"status": basic, "needs_investigation": basic != "up", "checks": [],
                             "intent": {"device": "ncsa-data-sw", **intent}}}


V6 = {"gateway-ipv6": {"address": "2602:fcfb:9:15::1", "netmask": 64},
      "external-access": {"ip-type": "v6", "permit-ipv6": [{"id": 10, "address": "2602:fcfb:9:15::2"}]}}
BOTH = {"gateway-ipv4": {"address": "10.148.1.1", "netmask": 24}, "gateway-ipv6": {"address": "2001:db8::1", "netmask": 64}}


def _report(*services: dict, verdicts: dict[str, str]) -> str:
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = ["ncsa-data-sw"]
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services", "payload": {"extra": {
        "services": {f"{s['service_type']}/{s['name']}": s for s in services}}}})
    for service in services:
        status = verdicts.get(service["name"])
        if status:
            case.diagnoses.append({"kind": "dataplane", "source": "llm", "status": status, "complete": True,
                                   "dataplane_status": status, "observed": "o", "cause": "c",
                                   "subject": {"name": service["name"], "service_type": service["service_type"]}})
            case.service_coverage[service["name"]] = "investigated"
    return render_report(case, summary="s", duration_seconds=1, dry_run=True, llm_model="m")


def _section(report: str, name: str) -> str:
    return report.split(f"· {name}\n", 1)[1].split("\n### ", 1)[0]


def test_clear_l3rt_gets_the_ping_an_operator_can_run_and_says_the_agent_did_not():
    report = _report(_l3rt("external", V6), _l3rt("internal", BOTH), verdicts={"external": "up", "internal": "up"})

    assert ("**Delivery check for an operator (not run by this agent):** from permitted host "
            "`2602:fcfb:9:15::2`, `ping 2602:fcfb:9:15::1`.") in _section(report, "external")
    assert ("**Delivery check for an operator (not run by this agent):** from a host in `10.148.1.0/24`, "
            "`ping 10.148.1.1`; from a host in `2001:db8::/64`, `ping 2001:db8::1`.") in _section(report, "internal")


def test_no_delivery_check_for_a_service_that_is_not_up_or_has_no_gateway():
    report = _report(_l3rt("faulted", V6), _l3rt("unresolved", V6), _l3rt("bridged", {}, kind="l2bridge"),
                     verdicts={"faulted": "down", "bridged": "up"})

    assert "Delivery check for an operator" not in report
