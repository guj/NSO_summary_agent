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


@pytest.mark.asyncio
async def test_a_verdict_that_did_not_pass_keeps_what_the_device_answered_and_a_pass_keeps_only_its_command():
    async def call(client, tool, args):
        return {"status": "success", "data": {"result": "Hu0/0/0/1 is down, line protocol is down\nLast flapped 13w1d"}}

    probe = Probe(None, call, {})
    down = await probe.show("a", "interfaces Hu0/0/0/1")
    probe.check("attachment", "fault", "Hu0/0/0/1", "a", read=down)
    probe.check("other", "pass", "fine", "a", read=down)

    by_name = {c["check"]: c for c in probe.checks}
    [source] = by_name["attachment"]["sources"]
    assert source["excerpt"] == "Hu0/0/0/1 is down, line protocol is down\nLast flapped 13w1d"
    assert "excerpt" not in by_name["other"]["sources"][0]            # a pass keeps only its command


@pytest.mark.asyncio
async def test_a_failed_read_keeps_why_it_failed():
    async def call(client, tool, args):
        if args["device_name"] == "brist":
            return {"status": "error", "error_message": "RESTCONF 500: Device brist is southbound locked"}
        return {"status": "success", "data": {"result": "% Invalid input detected at '^' marker."}}

    probe = Probe(None, call, {})
    locked = await probe.show("brist", "interfaces Hu0/0/0/1")
    refused = await probe.show("a", "interfaces Hu0/0/0/2")

    assert locked == "" and refused == ""                              # still no evidence for the verdicts
    by_cmd = {c["sources"][0]["command"]: c["sources"][0] for c in probe.checks}
    assert by_cmd["interfaces Hu0/0/0/1"]["excerpt"] == "RESTCONF 500: Device brist is southbound locked"
    assert by_cmd["interfaces Hu0/0/0/1"]["available"] is False
    assert by_cmd["interfaces Hu0/0/0/2"]["excerpt"] == "% Invalid input detected at '^' marker."


@pytest.mark.asyncio
async def test_a_long_reply_is_cut_between_lines_and_says_so():
    from diagnostic_mas.operational_checks.probe import EXCERPT_LIMIT

    lines = [f"line {n} of a long reply" for n in range(400)]

    async def call(client, tool, args):
        return {"status": "success", "data": {"result": "\n".join(lines)}}

    probe = Probe(None, call, {})
    long = await probe.show("a", "l2vpn bridge-domain detail")
    probe.check("bridge_state", "unknown", "x", "a", read=long)

    [source] = probe.checks[0]["sources"]
    assert source["truncated"] is True and len(source["excerpt"]) <= EXCERPT_LIMIT
    assert source["excerpt"].endswith("line 59 of a long reply") or source["excerpt"].splitlines()[-1] in lines


@pytest.mark.asyncio
async def test_a_verdict_can_show_the_block_it_was_read_from_instead_of_the_whole_reply():
    async def call(client, tool, args):
        return {"status": "success", "data": {"result": "block one\n\nblock two: the one that matters\n\nblock three"}}

    probe = Probe(None, call, {})
    bulk = await probe.show("a", "l2vpn bridge-domain detail")
    probe.check("gateway_identity", "unknown", "x", "a", read=bulk, shown="block two: the one that matters")

    assert probe.checks[0]["sources"][0]["excerpt"] == "block two: the one that matters"


@pytest.mark.asyncio
async def test_a_gateway_that_cannot_be_identified_shows_the_bridge_domain_it_looked_in():
    outputs = {("a", k): v for k, v in bridge_outputs().items()}                # an AC and no BVI in the bridge-domain
    result = await run_check(l3rt, endpoint("a") | {"gateway-ipv4": {"address": "192.0.2.1", "netmask": 24}}, outputs)

    [verdict] = [c for c in result["checks"] if c["check"] == "gateway_identity"]
    assert verdict["status"] == "unknown"
    [source] = verdict["sources"]
    assert "AC: HundredGigE0/0/0/1.100, state is up" in source["excerpt"] and "BVI" not in source["excerpt"]


@pytest.mark.asyncio
async def test_a_service_that_passes_keeps_no_excerpts():
    inst = endpoint("a") | {"gateway-ipv4": {"address": "192.0.2.1", "netmask": 24}}
    out = await evaluate(record("l3rt", ["a"]) | {"service_type": "l3rt"}, inst, None,
                         call_from(routed_outputs(), []), {})

    assert out["status"] == "up" and not any("excerpt" in s for c in out["checks"] for s in c.get("sources", []))


def test_a_verdict_that_did_not_pass_shows_what_the_device_answered_under_its_command():
    from diagnostic_mas.html_report import render_html_report

    html = render_html_report(REPORT, "r1", case=_case([
        {"check": "attachment", "status": "fault", "device": "mich-data-sw", "observation": "Hu0/0/0/24/1.0",
         "sources": [{"device": "mich-data-sw", "command": "interfaces Hu0/0/0/24/1.0",
                      "collected_at": "2026-10-20T19:29:35+00:00", "available": True,
                      "excerpt": "Hu0/0/0/24/1.0 is down, line protocol is down <b>\nLast flapped 13w1d", "truncated": True}]},
        {"check": "bridge_scope", "status": "unknown", "device": None, "observation": "x",
         "sources": [{"device": "brist-data-sw", "command": "isis neighbors", "available": False,
                      "excerpt": "RESTCONF 500: Device brist-data-sw is southbound locked"}]},
    ]))

    # The reply is text, never markup, and says when it was cut.
    assert ('<pre class="reply">Hu0/0/0/24/1.0 is down, line protocol is down &lt;b&gt;\n'
            'Last flapped 13w1d</pre><p class="cut">cut here; the rest of the reply is not kept</p>') in html
    assert '<pre class="reply">RESTCONF 500: Device brist-data-sw is southbound locked</pre>' in html
    assert "<b>" not in html.split("Collector detail")[1].split("</details>")[0]


# A real IOS XR bridge-domain block (newy-data-sw, 11 Nov): about 30 lines of settings, then the members.
BRIDGE_BLOCK = (
    "Bridge group: bg-fabric_network-9f80934b-e13d, bridge-domain: bd-fabric_network-9f80934b-, id: 12, state: up, ShgId: 0, MSTi: 0\n"
    + "".join(f"  Setting number {n}: some value that is not about membership at all\n" for n in range(1, 61))
    + "  Bridge MTU: 1500\n  Create time: 09/11/2026 20:21:15 (2d02h ago)\n"
    "  ACs: 2 (2 up), VFIs: 0, PWs: 0 (0 up), PBBs: 0 (0 up), VNIs: 0 (0 up)\n"
    "  List of EVPNs:\n    EVPN, state: up\n      evi: 9063 (MPLS)\n"
    "  List of ACs:\n    AC: HundredGigE0/0/0/7.2115, state is up\n      Type VLAN; Num Ranges: 1\n"
    "    AC: FourHundredGigE0/0/0/60.2026, state is up\n      Type VLAN; Num Ranges: 1\n"
)


def test_a_long_bridge_block_is_shown_by_its_header_and_its_members_not_its_settings():
    from diagnostic_mas.operational_checks.probe import EXCERPT_LIMIT, bridge_view

    shown = bridge_view(BRIDGE_BLOCK)

    assert shown.splitlines()[0].startswith("Bridge group: bg-fabric_network-9f80934b-e13d, bridge-domain:")
    assert "ACs: 2 (2 up)" in shown and "AC: HundredGigE0/0/0/7.2115, state is up" in shown
    assert "AC: FourHundredGigE0/0/0/60.2026" in shown and "Setting number 7" not in shown and len(shown) < EXCERPT_LIMIT
    short = "Bridge group: g, bridge-domain: d, id: 0, state: up\n  AC: Hu0/0/0/1.100, state is up\n"
    assert bridge_view(short) == short                                      # a short block is shown whole
    assert bridge_view("no markers here\n" * 200) == "no markers here\n" * 200   # nothing to go by: unchanged


@pytest.mark.asyncio
async def test_a_gateway_that_cannot_be_identified_shows_the_members_of_a_long_bridge_domain():
    outputs = {("a", k): v for k, v in bridge_outputs().items()}
    outputs["a", "l2vpn bridge-domain group bg-exact detail"] = BRIDGE_BLOCK.replace(
        "bg-fabric_network-9f80934b-e13d", "bg-exact").replace("bd-fabric_network-9f80934b-", "bd-short-")
    result = await run_check(l3rt, endpoint("a") | {"gateway-ipv4": {"address": "192.0.2.1", "netmask": 24}}, outputs)

    [verdict] = [c for c in result["checks"] if c["check"] == "gateway_identity"]
    excerpt = verdict["sources"][0]["excerpt"]
    assert "List of ACs:" in excerpt and "AC: HundredGigE0/0/0/7.2115" in excerpt and "BVI" not in excerpt
    assert "Setting number 7" not in excerpt


def test_a_block_whose_settings_end_near_the_limit_still_shows_its_members():
    """test71's cut: the first 1,500 characters ended exactly at "List of ACs:", before any AC line."""
    from diagnostic_mas.operational_checks.probe import EXCERPT_LIMIT, bridge_view, excerpt

    header = "Bridge group: bg-x, bridge-domain: bd-y, id: 7, state: up, ShgId: 0, MSTi: 0\n"
    settings = "".join(f"  Setting {n:02d}: value value value value value\n" for n in range(1, 80))
    settings = settings[:EXCERPT_LIMIT - len(header) - 60].rsplit("\n", 1)[0] + "\n"       # ends just under the limit
    members = ("  ACs: 2 (1 up), VFIs: 0, PWs: 0 (0 up), PBBs: 0 (0 up), VNIs: 0 (0 up)\n  List of ACs:\n"
               "    AC: HundredGigE0/0/0/17.0, state is down (Segment-down)\n      Type VLAN; Num Ranges: 1\n"
               "    AC: BVI4256, state is up\n      Type Routed-Interface\n")
    block = header + settings + members
    assert len(header + settings) < EXCERPT_LIMIT < len(block)

    assert "AC: BVI4256" not in excerpt(block)[0]                          # a plain cut stops before the members
    shown, cut = excerpt(bridge_view(block))
    assert "AC: HundredGigE0/0/0/17.0, state is down" in shown and "AC: BVI4256, state is up" in shown and not cut


@pytest.mark.asyncio
async def test_a_mirror_source_that_is_not_operational_shows_its_own_session_not_the_devices_whole_answer():
    from diagnostic_mas.operational_checks.probe import EXCERPT_LIMIT
    from test_operational_port_mirror_generic import STATUS as MIRROR_STATUS, session as mirror_session, source as mirror_source

    others = "".join(mirror_session(f"mon_other_{n}", f"TwentyFiveGigE0/0/0/{n}",
                                    mirror_source(f"HundredGigE0/0/0/{20 + n}"), mirror_source(f"HundredGigE0/0/0/{40 + n}"))
                     for n in range(1, 8))
    mine = mirror_session("mon_pm1", "HundredGigE0/0/0/9", mirror_source("HundredGigE0/0/0/2", "Not operational (source state down)."))
    answer = others + mine                                              # the one that matters is the last of eight
    assert len(answer) > 2 * EXCERPT_LIMIT

    outputs = {("a", MIRROR_STATUS): answer, ("a", "interfaces Hu0/0/0/9"): "Hu0/0/0/9 is up, line protocol is up",
               ("a", "interfaces Hu0/0/0/2"): "Hu0/0/0/2 is down, line protocol is down"}
    from test_operational_port_mirror_generic import mirror
    result = await run_check(port_mirror, mirror(**{"from-interface-vlan": []}), outputs)

    [verdict] = [c for c in result["checks"] if c["check"] == "source"]
    excerpt = verdict["sources"][0]["excerpt"]
    assert verdict["status"] == "fault"
    assert "Monitor-session mon_pm1" in excerpt and "Not operational (source state down)." in excerpt
    assert "mon_other_3" not in excerpt


@pytest.mark.asyncio
async def test_a_mirror_session_that_is_not_listed_shows_which_sessions_the_device_did_list():
    from test_operational_port_mirror_generic import STATUS as MIRROR_STATUS, mirror, session as mirror_session, source as mirror_source

    answer = "".join(mirror_session(f"mon_other_{n}", f"TwentyFiveGigE0/0/0/{n}", mirror_source(f"HundredGigE0/0/0/{n}"))
                     for n in range(1, 12))
    outputs = {("a", MIRROR_STATUS): answer}
    result = await run_check(port_mirror, mirror(), outputs)

    [verdict] = [c for c in result["checks"] if c["check"] == "session"]
    excerpt = verdict["sources"][0]["excerpt"]
    assert verdict["status"] == "unknown"
    assert "Monitor-session mon_other_1" in excerpt and "Monitor-session mon_other_11" in excerpt   # all of them, by name
    assert "Direction:" not in excerpt                                       # without their fields


@pytest.mark.asyncio
async def test_a_forwarding_reply_longer_than_the_limit_shows_its_header_and_the_acs_own_line():
    from diagnostic_mas.operational_checks.probe import EXCERPT_LIMIT

    forwarding = ("Bridge-domain name: bg-exact:bd-short-, id: 0, state: up\n"
                  + "".join(f"  Setting {n}: some value that is not about forwarding members\n" for n in range(1, 60))
                  + "  Hu0/0/0/1.100, state: oper down (hardware programming failed)\n")
    assert len(forwarding) > EXCERPT_LIMIT
    outputs = {("a", k): v for k, v in bridge_outputs().items()}
    outputs["a", FORWARDING] = forwarding
    result = await run_check(l2bridge, endpoint("a"), outputs)

    [verdict] = [c for c in result["checks"] if c["check"] == "local_forwarding"]
    excerpt = verdict["sources"][0]["excerpt"]
    assert verdict["status"] == "fault"
    assert "Bridge-domain name: bg-exact:bd-short-, id: 0, state: up" in excerpt
    assert "Hu0/0/0/1.100, state: oper down (hardware programming failed)" in excerpt and "Setting 30" not in excerpt


@pytest.mark.asyncio
async def test_a_verdict_that_read_several_commands_keeps_a_useful_share_of_each():
    from diagnostic_mas.operational_checks.probe import EXCERPT_LIMIT

    async def call(client, tool, args):
        return {"status": "success", "data": {"result": f"{args['input_command']}\n" + "x" * 3000}}

    probe = Probe(None, call, {})
    reads = [await probe.show("a", f"cmd {n}") for n in range(3)]
    probe.check("label_path", "unknown", "x", "a", read=reads)

    shares = [len(src["excerpt"]) for src in probe.checks[0]["sources"]]
    assert min(shares) >= 1200 and max(shares) <= EXCERPT_LIMIT


def _basic_with_replies():
    return {"status": "unknown", "checks": [
        {"check": "gateway_identity", "status": "unknown", "device": "a", "observation": "x",
         "looked_for": "exactly one BVI among the members",
         "sources": [{"device": "a", "command": "l2vpn bridge-domain detail", "available": True,
                      "collected_at": "2026-11-11T22:38:38+00:00", "excerpt": "Bridge group: bg, bridge-domain: bd", "truncated": True}]},
        {"check": "attachment", "status": "pass", "device": "a", "observation": "Hu0/0/0/1.100"}]}


def test_the_model_gets_the_checks_and_their_commands_but_not_the_replies_kept_for_the_report():
    from diagnostic_mas.drill import _issue_brief
    from diagnostic_mas.dataplane_verify import _service_brief
    from diagnostic_mas.operational_checks.common import checks_for_llm

    basic = _basic_with_replies()
    given = checks_for_llm(basic)

    [source] = given["checks"][0]["sources"]
    assert source["command"] == "l2vpn bridge-domain detail" and "excerpt" not in source and "truncated" not in source
    assert "looked_for" not in given["checks"][0] and given["checks"][1] == basic["checks"][1]
    assert "excerpt" in basic["checks"][0]["sources"][0]                    # the saved record is untouched

    record = {"service_type": "l3rt", "name": "net1", "devices": ["a"], "basic_checks": basic}
    assert "excerpt" not in str(_service_brief(record)["basic_checks"])
    issue = {"id": "is_1", "code": "service_down", "message": "m", "status": "open", "basic_checks": basic}
    assert "excerpt" not in str(_issue_brief(issue))


@pytest.mark.asyncio
async def test_a_verdict_that_did_not_pass_says_what_the_check_looked_for_and_a_pass_does_not():
    async def call(client, tool, args):
        return {"status": "success", "data": {"result": "some reply"}}

    probe = Probe(None, call, {})
    reading = await probe.show("a", "l2vpn bridge-domain detail")
    probe.check("gateway_identity", "unknown", "x", "a", read=reading)
    probe.check("bridge_state", "pass", "bg:bd", "a", read=reading)
    probe.check("not_a_known_check", "unknown", "x", "a", read=reading)

    by_name = {c["check"]: c for c in probe.checks}
    assert "exactly one BVI" in by_name["gateway_identity"]["looked_for"]
    assert "looked_for" not in by_name["bridge_state"] and "looked_for" not in by_name["not_a_known_check"]


def test_what_a_check_looked_for_is_shown_above_its_commands_in_the_html():
    from diagnostic_mas.html_report import render_html_report

    html = render_html_report(REPORT, "r1", case=_case([
        {"check": "attachment", "status": "fault", "device": "mich-data-sw", "observation": "Hu0/0/0/24/1.0",
         "looked_for": "the attachment's interface up, line protocol up <b>",
         "sources": [{"device": "mich-data-sw", "command": "interfaces Hu0/0/0/24/1.0", "available": True,
                      "collected_at": "2026-10-20T19:29:35+00:00", "excerpt": "Hu0/0/0/24/1.0 is down"}]},
        {"check": "bridge_scope", "status": "unknown", "device": None, "observation": "x"}]))

    assert '<p class="lookedfor">Looked for: the attachment&#x27;s interface up, line protocol up &lt;b&gt;</p><ul>' in html


@pytest.mark.asyncio
async def test_a_check_that_names_the_line_it_matched_shows_that_line_with_three_lines_either_side():
    from diagnostic_mas.operational_checks.probe import EXCERPT_LIMIT

    body = "".join(f"  counter {n}: {n * 7} packets of the kind that is not the point here\n" for n in range(1, 70))
    reply = "Hu0/0/0/1 is up, line protocol is up\n" + body + "  oper state: DOWN (the line that matters)\n" + body
    assert len(reply) > EXCERPT_LIMIT

    async def call(client, tool, args):
        return {"status": "success", "data": {"result": reply}}

    probe = Probe(None, call, {})
    reading = await probe.show("a", "interfaces Hu0/0/0/1")
    probe.check("attachment", "fault", "x", "a", read=reading, matched="oper state: DOWN (the line that matters)")

    excerpt = probe.checks[0]["sources"][0]["excerpt"]
    lines = excerpt.splitlines()
    assert lines[0] == "Hu0/0/0/1 is up, line protocol is up"                      # the header line
    assert "  oper state: DOWN (the line that matters)" in lines
    at = lines.index("  oper state: DOWN (the line that matters)")
    assert lines[at - 1].startswith("  counter 69") and lines[at - 3].startswith("  counter 67")      # three before
    assert lines[at + 1].startswith("  counter 1:") and lines[at + 3].startswith("  counter 3:")      # three after
    assert "counter 30" not in excerpt


MEMBER_DETAIL = (
    "      Type VLAN; Num Ranges: 1\n      Rewrite Tags: []\n      VLAN ranges: [{vlan}, {vlan}]\n      MTU 9202; XC ID 0x32e; interworking none\n"
    + "".join(f"      Setting {n}: some per-member value that is not about who the members are\n" for n in range(1, 25)))


def test_the_members_view_names_each_member_without_its_per_member_settings():
    from diagnostic_mas.operational_checks.probe import members_view

    block = (BRIDGE_BLOCK.split("  List of ACs:")[0]
             + "  List of ACs:\n    AC: FourHundredGigE0/0/0/60.2026, state is up\n" + MEMBER_DETAIL.format(vlan=2026)
             + "    AC: HundredGigE0/0/0/7.2115, state is up\n" + MEMBER_DETAIL.format(vlan=2115)
             + "    AC: BVI4256, state is down (Segment-down)\n      Type Routed-Interface\n")
    shown = members_view(block)

    assert shown.splitlines()[0].startswith("Bridge group: bg-fabric_network-9f80934b-e13d")
    assert "ACs: 2 (2 up)" in shown and "EVPN, state: up" in shown and "evi: 9063 (MPLS)" in shown
    assert [line.strip() for line in shown.splitlines() if "AC: " in line] == [
        "AC: FourHundredGigE0/0/0/60.2026, state is up", "AC: HundredGigE0/0/0/7.2115, state is up",
        "AC: BVI4256, state is down (Segment-down)"]
    assert "Setting 7" not in shown and "VLAN ranges" not in shown
    assert members_view("no markers here\n") == "no markers here\n"            # nothing to go by: unchanged


@pytest.mark.asyncio
async def test_a_gateway_that_cannot_be_identified_lists_every_member_even_when_each_has_long_settings():
    outputs = {("a", k): v for k, v in bridge_outputs().items()}
    block = ("Bridge group: bg-exact, bridge-domain: bd-short-, id: 12, state: up, ShgId: 0, MSTi: 0\n"
             "  ACs: 2 (2 up), VFIs: 0, PWs: 0 (0 up)\n  List of ACs:\n"
             "    AC: FourHundredGigE0/0/0/60.2026, state is up\n" + MEMBER_DETAIL.format(vlan=2026) * 3
             + "    AC: HundredGigE0/0/0/1.100, state is up\n      Type VLAN; Num Ranges: 1\n      VLAN ranges: [100, 100]\n")
    outputs["a", "l2vpn bridge-domain group bg-exact detail"] = block
    result = await run_check(l3rt, endpoint("a") | {"gateway-ipv4": {"address": "192.0.2.1", "netmask": 24}}, outputs)

    [verdict] = [c for c in result["checks"] if c["check"] == "gateway_identity"]
    excerpt = verdict["sources"][0]["excerpt"]
    assert "AC: FourHundredGigE0/0/0/60.2026, state is up" in excerpt and "AC: HundredGigE0/0/0/1.100, state is up" in excerpt
    assert "BVI" not in excerpt and "Setting 7" not in excerpt


def _found(result, check, device=None):
    """What the one non-passing verdict of ``check`` says it found."""
    [verdict] = [c for c in result["checks"] if c["check"] == check and c["status"] != "pass"
                 and (device is None or c.get("device") == device)]
    return verdict.get("found")


L3 = endpoint("a") | {"gateway-ipv4": {"address": "192.0.2.1", "netmask": 24}}


@pytest.mark.asyncio
async def test_a_gateway_that_cannot_be_identified_says_whether_none_or_several_were_found():
    none = await run_check(l3rt, L3, {("a", k): v for k, v in bridge_outputs().items()})
    assert _found(none, "gateway_identity") == "no BVI among the members of bg-exact:bd-short- (HundredGigE0/0/0/1.100)"

    two = {("a", k): v for k, v in bridge_outputs(bvi="BVI50000").items()}
    two["a", "l2vpn bridge-domain group bg-exact detail"] += " AC: BVI50001, state is up\n"
    several = await run_check(l3rt, L3, two)
    assert _found(several, "gateway_identity") == "2 BVIs among the members of bg-exact:bd-short-: BVI50000, BVI50001"

    unread = await run_check(l3rt, L3, {})
    assert _found(unread, "gateway_identity") == "the bridge-domain could not be read for the attachment"


@pytest.mark.asyncio
async def test_bridge_verdicts_say_what_was_found_instead_of_what_was_looked_for():
    outputs = {("a", k): v for k, v in bridge_outputs().items()}
    detail = outputs["a", "l2vpn bridge-domain group bg-exact detail"]
    outputs["a", "l2vpn bridge-domain group bg-exact detail"] = detail.replace("state is up", "state is down (Segment-down)").replace(
        "[100, 100]", "[200, 200]")
    outputs["a", "interfaces Hu0/0/0/1.100"] = "Hu0/0/0/1.100 is down, line protocol is down\n  MTU 9216"
    outputs["a", FORWARDING] = "Bridge-domain name: bg-exact:bd-short-, id: 0, state: up\n  Hu0/0/0/9.9, state: oper up\n"
    result = await run_check(l2bridge, endpoint("a"), outputs)

    assert _found(result, "attachment") == "Hu0/0/0/1.100 is down, line protocol is down"
    assert _found(result, "membership") == "AC: HundredGigE0/0/0/1.100, state is down (Segment-down)"
    assert _found(result, "encapsulation") == "VLAN ranges [200, 200]; intended 100"
    assert _found(result, "local_forwarding") == "no forwarding entry for Hu0/0/0/1.100 in the reply"
    assert "found" not in [c for c in result["checks"] if c["status"] == "pass"][0]          # a pass says nothing


@pytest.mark.asyncio
async def test_route_and_gateway_verdicts_say_what_the_device_answered():
    outputs = routed_outputs()
    outputs["a", "route 192.0.2.0/24"] = "% Network not in table"
    outputs["a", "running-config interface BVI50000"] = "interface BVI50000\n ipv4 address 198.51.100.1 255.255.255.0\n!\n"
    outputs["a", "interfaces BVI50000"] = "BVI50000 is up, line protocol is down"
    result = await run_check(l3rt, L3, outputs)

    assert _found(result, "gateway") == "BVI50000 is up, line protocol is down"
    assert _found(result, "gateway_address") == "192.0.2.1 is not configured on BVI50000; configured: ipv4 address 198.51.100.1 255.255.255.0"
    assert _found(result, "rib") == "% Network not in table"


@pytest.mark.asyncio
async def test_a_default_route_only_and_a_missing_cef_entry_are_named():
    outputs = routed_outputs()
    outputs["a", "route 192.0.2.0/24"] = 'Routing entry for 0.0.0.0/0\n Known via "static"\n via 10.0.0.1'
    result = await run_check(l3rt, L3, outputs)
    assert _found(result, "rib") == "only the default route 0.0.0.0/0 covers it"

    outputs = routed_outputs()
    outputs["a", "cef 192.0.2.0/24"] = "% No such route"
    cef = await run_check(l3rt, L3, outputs)
    assert _found(cef, "fib") == "no CEF entry for 192.0.2.0/24 in the reply (it begins: % No such route)"


def test_what_was_found_is_shown_below_what_was_looked_for_in_the_html_and_the_model_does_not_get_it():
    from diagnostic_mas.html_report import render_html_report
    from diagnostic_mas.operational_checks.common import checks_for_llm

    check = {"check": "attachment", "status": "fault", "device": "mich-data-sw", "observation": "Hu0/0/0/24/1.0",
             "looked_for": "the interface up", "found": "Hu0/0/0/24/1.0 is down <b>",
             "sources": [{"device": "mich-data-sw", "command": "interfaces brief", "available": True,
                          "collected_at": "2026-10-20T19:29:35+00:00", "excerpt": "x"}]}
    html = render_html_report(REPORT, "r1", case=_case([check, {"check": "bridge_scope", "status": "unknown",
                                                                  "device": None, "observation": "x"}]))

    assert ('<p class="lookedfor">Looked for: the interface up</p><p class="lookedfor">Found: '
            'Hu0/0/0/24/1.0 is down &lt;b&gt;</p><ul>') in html
    assert "found" not in checks_for_llm({"checks": [check]})["checks"][0]


def test_what_a_device_began_its_reply_with_skips_the_clock_line_and_the_prompt():
    from diagnostic_mas.operational_checks.probe import first_line

    assert first_line("Thu Nov 12 03:00:39.002 UTC\n\n% Network not in table\n\nRP/0/RP0/CPU0:star-data-sw#") == "% Network not in table"
    assert first_line("Thu Nov 12 03:07:39.458 UTC\nInterface not found (BVI4119)\n\nRP/0/RP0/CPU0:kans-data-sw#") == "Interface not found (BVI4119)"
    # Nothing but the clock and the prompt: the device said nothing.
    assert first_line("Thu Nov 12 03:07:38.952 UTC\nRP/0/RP0/CPU0:kans-data-sw#") is None


@pytest.mark.asyncio
async def test_a_reply_with_nothing_in_it_is_called_empty_in_the_found_line():
    outputs = routed_outputs()
    outputs["a", "route 192.0.2.0/24"] = "Thu Nov 12 03:00:39.002 UTC\n\n% Network not in table\n\nRP/0/RP0/CPU0:a#"
    outputs["a", "interfaces BVI50000"] = "Thu Nov 12 03:07:39.458 UTC\nInterface not found (BVI50000)\n\nRP/0/RP0/CPU0:a#"
    result = await run_check(l3rt, L3, outputs)
    assert _found(result, "rib") == "% Network not in table"
    assert _found(result, "gateway") == "no state line for BVI50000 in the reply (it begins: Interface not found (BVI50000))"


@pytest.mark.asyncio
async def test_an_empty_forwarding_reply_is_called_empty():
    outputs = {("a", k): v for k, v in bridge_outputs().items()}
    outputs["a", FORWARDING] = "Thu Nov 12 03:07:38.952 UTC\nRP/0/RP0/CPU0:a#"
    result = await run_check(l2bridge, endpoint("a"), outputs)
    assert _found(result, "local_forwarding") == "the reply is not the up forwarding table of bg-exact:bd-short- (it holds nothing)"
