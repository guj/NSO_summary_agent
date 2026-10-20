"""Each fixed-check verdict names the command(s) it was read from, so the report can show them."""
import pytest

from diagnostic_mas.operational_checks import l2bridge, l2sts, l3rt, port_mirror
from diagnostic_mas.operational_checks.probe import Probe
from diagnostic_mas.operational_checks.runner import evaluate
from test_operational_port_mirror_generic import device_answers, mirror
from test_operational_service_types import (
    bridge_outputs, call_from, endpoint, record, routed_outputs, sts_outputs)

FORWARDING = "l2vpn forwarding bridge-domain bg-exact:bd-short- detail location 0/RP0/CPU0"


def commands(result, check, device=None):
    """The commands behind each recorded verdict of one check, in order."""
    return [[s["command"] for s in c.get("sources", [])] for c in result["checks"]
            if c["check"] == check and (device is None or c.get("device") == device)]


async def run_check(module, instance, outputs):
    """One type's verdicts as its check module records them, before the runner sums them up."""
    probe = Probe(None, call_from(outputs, []), {})
    await module.check(instance, probe)
    return {"checks": probe.checks}


def reads(result, check, device):
    """(device, command) behind the one verdict of ``check`` recorded on ``device``."""
    [found] = [c for c in result["checks"] if c["check"] == check and c.get("device") == device]
    return [(s["device"], s["command"]) for s in found["sources"]]


@pytest.mark.asyncio
async def test_probe_reads_carry_their_command_into_the_verdicts_that_use_them():
    async def call(client, tool, args):
        text = {"interfaces Hu0/0/0/1": "Hu0/0/0/1 is up, line protocol is up"}.get(
            args["input_command"], "% Invalid input")
        return {"status": "success", "data": {"result": text}}

    probe = Probe(None, call, {})
    up = await probe.show("a", "interfaces Hu0/0/0/1")
    missing = await probe.show("a", "interfaces Hu0/0/0/2")
    probe.check("attachment", "pass", "Hu0/0/0/1", "a", read=up)
    probe.check("pair", "unknown", "both", "a", read=[up, missing, up, None])
    probe.check("intent", "unknown", "from the service definition")

    by_name = {c["check"]: c for c in probe.checks}
    assert by_name["attachment"]["sources"] == [{
        "device": "a", "command": "interfaces Hu0/0/0/1",
        "collected_at": up.source["collected_at"], "available": True}]
    assert [(s["command"], s["available"]) for s in by_name["pair"]["sources"]] == [
        ("interfaces Hu0/0/0/1", True), ("interfaces Hu0/0/0/2", False)]
    assert "sources" not in by_name["intent"]
    # An empty answer is recorded against its own command.
    assert by_name["collection"]["sources"][0]["command"] == "interfaces Hu0/0/0/2"
    # The output is still the plain text the checks parse.
    assert up == "Hu0/0/0/1 is up, line protocol is up"


@pytest.mark.asyncio
async def test_bridge_verdicts_name_the_scoped_reads_they_parsed():
    outputs = {("a", k): v for k, v in bridge_outputs().items()}
    result = await run_check(l2bridge, endpoint("a"), outputs)

    assert commands(result, "attachment") == [["interfaces Hu0/0/0/1.100"]]
    for check in ("bridge_state", "membership", "encapsulation"):
        assert commands(result, check) == [["l2vpn bridge-domain group bg-exact detail"]], check
    assert commands(result, "local_forwarding") == commands(result, "local_flooding") == [[FORWARDING]]


@pytest.mark.asyncio
async def test_bridge_verdicts_name_the_bulk_reads_when_those_answered():
    outputs = {("a", k): v for k, v in bridge_outputs().items()}
    outputs["a", "interfaces brief"] = "Hu0/0/0/1.100          up          up    ARPA  9216  100000000\n"
    outputs["a", "l2vpn bridge-domain detail"] = (
        outputs["a", "l2vpn bridge-domain group bg-exact detail"] + "\nRP/0/RP0/CPU0:pe#")
    result = await run_check(l2bridge, endpoint("a"), outputs)

    assert commands(result, "attachment") == [["interfaces brief"]]
    for check in ("bridge_state", "membership", "encapsulation"):
        assert commands(result, check) == [["l2vpn bridge-domain detail"]], check
    assert commands(result, "local_forwarding") == [[FORWARDING]]


@pytest.mark.asyncio
async def test_evpn_verdicts_name_the_reads_from_both_ends():
    detail, imet = "evpn evi vpn-id 9001 detail", "evpn evi vpn-id 9001 inclusive-multicast detail"
    instance = {"site-a": endpoint("a"), "site-z": endpoint("b")}
    result = await run_check(l2sts, instance, sts_outputs())

    assert commands(result, "effective_evpn", "a") == [[detail]]
    # What a imports against what b exports; b's label and address against a's received routes.
    assert reads(result, "rt_compatibility", "a") == [("a", detail), ("b", detail)]
    assert reads(result, "replication", "a") == [("a", imet), ("b", detail)]
    assert commands(result, "transport", "a") == [["cef 10.0.0.2/32"]]


@pytest.mark.asyncio
async def test_gateway_and_route_verdicts_name_their_commands():
    instance = endpoint("a") | {"gateway-ipv4": {"address": "192.0.2.1", "netmask": 24}}
    result = await run_check(l3rt, instance, routed_outputs())

    assert commands(result, "gateway") == [["interfaces BVI50000"]]
    assert commands(result, "gateway_address") == [["running-config interface BVI50000"]]
    assert commands(result, "rib") == commands(result, "gateway_route_binding") == [["route 192.0.2.0/24"]]
    assert commands(result, "fib") == [["cef 192.0.2.0/24"]]


@pytest.mark.asyncio
async def test_label_path_names_both_xconnect_reads_and_the_forwarding_entry():
    from test_l2ptp_label_path import CERN, DETAIL, LOOKUP, UCSD, run, saved

    result, _ = await run(saved())

    assert [c["sources"] for c in result["checks"] if c["check"] == "xconnect"] == [
        [{"device": CERN, "command": "l2vpn xconnect"}], [{"device": UCSD, "command": "l2vpn xconnect"}]]
    assert reads(result, "label_path", CERN) == [(CERN, DETAIL), (UCSD, DETAIL), (CERN, LOOKUP)]
    assert reads(result, "label_path", UCSD) == [(UCSD, DETAIL), (CERN, DETAIL)]


@pytest.mark.asyncio
async def test_mirror_and_interface_verdicts_name_their_commands():
    result = await run_check(port_mirror, mirror(), device_answers())

    assert commands(result, "session") == [["monitor-session status detail"]]
    assert commands(result, "source") == [["monitor-session status detail"]] * 2
    assert commands(result, "interface") == [
        ["interfaces Hu0/0/0/9"], ["interfaces Hu0/0/0/2"], ["interfaces Hu0/0/0/3.100"]]


@pytest.mark.asyncio
async def test_only_services_the_report_lists_keep_their_commands():
    outputs = {("a", k): v for k, v in bridge_outputs().items()}
    passed = await evaluate(record("l2bridge", ["a"]), endpoint("a"), None, call_from(outputs, []), {})
    outputs["a", FORWARDING] = "% Invalid input"
    failed = await evaluate(record("l2bridge", ["a"]), endpoint("a"), None, call_from(outputs, []), {})

    # A passing service has no Collector detail in the report, so its verdicts carry no commands.
    assert passed["status"] == "up" and not any("sources" in c for c in passed["checks"])
    assert failed["status"] == "unknown" and commands(failed, "attachment") == [["interfaces Hu0/0/0/1.100"]]
    # The service still lists every command its check ran.
    assert passed["sources"]


REPORT = """# NSO Diagnostic Report

## Services

### L2BRIDGE · svc1

**Result:** Confirmed fault — dataplane down

**Collector detail:**
Basic check attachment (fault): Hu0/0/0/24/1.0
Basic check bridge_scope (unknown): Local bridge fast path requires exactly one verified device/domain

*Service ID: `svc1`*
"""


def _case(checks):
    record = {"name": "svc1", "basic_checks": {"checks": checks}}
    return {"evidence": [{"kind": "spine", "role": "service",
                          "payload": {"extra": {"services": {"l2bridge/svc1": record}}}}]}


def test_each_collector_line_opens_to_the_commands_its_verdict_read():
    from diagnostic_mas.html_report import render_html_report

    html = render_html_report(REPORT, "r1", case=_case([
        {"check": "attachment", "status": "fault", "device": "mich-data-sw", "observation": "Hu0/0/0/24/1.0",
         "sources": [{"device": "mich-data-sw", "command": "interfaces brief",
                      "collected_at": "2026-10-20T19:29:34.697185+00:00", "available": True},
                     {"device": "mich-data-sw", "command": "interfaces Hu0/0/0/24/1.0",
                      "collected_at": "2026-10-20T19:29:35+00:00", "available": False}]},
        {"check": "bridge_scope", "status": "unknown", "device": None,
         "observation": "Local bridge fast path requires exactly one verified device/domain"},
    ]))

    assert ('<details class="check"><summary>Basic check attachment (fault): Hu0/0/0/24/1.0</summary>'
            '<ul><li><code>show interfaces brief</code> on mich-data-sw, read 19:29:34 UTC</li>'
            '<li><code>show interfaces Hu0/0/0/24/1.0</code> on mich-data-sw, read 19:29:35 UTC (no output)</li>'
            '</ul></details>') in html
    # A verdict from the service definition has no command to show.
    assert ('<div class="check">Basic check bridge_scope (unknown): '
            'Local bridge fast path requires exactly one verified device/domain</div>') in html


def test_collector_lines_stay_plain_when_they_do_not_match_the_recorded_verdicts():
    from diagnostic_mas.html_report import render_html_report

    html = render_html_report(REPORT, "r1", case=_case([
        {"check": "membership", "status": "pass", "device": "a", "observation": "x",
         "sources": [{"device": "a", "command": "l2vpn bridge-domain detail"}]}]))

    assert "show l2vpn bridge-domain detail" not in html
    assert "Basic check attachment (fault): Hu0/0/0/24/1.0" in html
