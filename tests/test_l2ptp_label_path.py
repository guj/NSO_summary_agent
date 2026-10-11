"""L2PTP fixed check: the service label is followed in each direction.

Device output is as two PEs returned it in a scan of 27 September 2026. On that
circuit the label one end shows for the far end is a local entry that is sent
as the far end's own label, which an earlier reading had taken for a fault.
"""
import pytest

from diagnostic_mas.operational_checks.l2ptp import evaluate_with_labels, forwarded, xc_labels
from diagnostic_mas.operational_checks.probe import Probe

CERN, UCSD = "cern-data-sw", "ucsd-data-sw"
DETAIL = "l2vpn xconnect group evpn_vpws xc-name evpn_vpws_9001 detail"
LOOKUP = "mpls forwarding labels 24152"

CERN_DETAIL = """

Sun Sep 27 05:25:37.656 UTC

Group evpn_vpws, XC evpn_vpws_9001, state is up; Interworking none
  AC: HundredGigE0/0/0/6.864, state is up
    Type VLAN; Num Ranges: 1
    Rewrite Tags: [3149]
    VLAN ranges: [864, 864]
    MTU 9206; XC ID 0x1bd2; interworking none
    Statistics:
      packets: received 419968291, sent 2569993516
      bytes: received 393840144195, sent 19379660585358
      drops: illegal VLAN 0, illegal length 0
  EVPN: neighbor 24152, PW ID: evi 9019, ac-id 3149, state is up ( established )
    XC ID 0xc0000001
    Encapsulation MPLS
    Encap type Ethernet, control word enabled
    Sequencing not set
    Ignore MTU mismatch: Enabled
    Transmit MTU zero: Enabled
    LSP : Up

      EVPN         Local                          Remote
      ------------ ------------------------------ -----------------------------
      Label        24005                          24152
      MTU          9220                           unknown
      Control word enabled                        enabled
      AC ID        864                            3149
      EVPN type    Ethernet                       Ethernet

      ------------ ------------------------------ -----------------------------
    Create time: 05/09/2026 00:24:04 (3w1d ago)
    Last time status changed: 05/09/2026 00:24:07 (3w1d ago)
    Statistics:
      packets: received 2569993516, sent 419968291
      bytes: received 19379660585358, sent 393840144195
RP/0/RP0/CPU0:cern-data-sw#"""

UCSD_DETAIL = """

Sun Sep 27 05:25:34.957 UTC

Group evpn_vpws, XC evpn_vpws_9001, state is up; Interworking none
  AC: Bundle-Ether100.3149, state is up
    Type VLAN; Num Ranges: 1
    Rewrite Tags: [864]
    VLAN ranges: [3149, 3149]
    MTU 9206; XC ID 0xa0000001; interworking none
    Statistics:
      packets: received 2047060614, sent 6126959084
      bytes: received 18208987433411, sent 9276775730586
      drops: illegal VLAN 0, illegal length 0
  EVPN: neighbor 10.143.0.1, PW ID: evi 9019, ac-id 864, state is up ( established )
    XC ID 0xc0000001
    Encapsulation MPLS
    Encap type Ethernet, control word enabled
    Sequencing not set
    Ignore MTU mismatch: Enabled
    Transmit MTU zero: Enabled
    LSP : Up

      EVPN         Local                          Remote
      ------------ ------------------------------ -----------------------------
      Label        24167                          24005
      MTU          9220                           unknown
      Control word enabled                        enabled
      AC ID        3149                           864
      EVPN type    Ethernet                       Ethernet

      ------------ ------------------------------ -----------------------------
    Create time: 05/09/2026 00:24:04 (3w1d ago)
    Last time status changed: 05/09/2026 00:24:05 (3w1d ago)
    Statistics:
      packets: received 6126959084, sent 2047060614
      bytes: received 9276775730586, sent 18208987433411
RP/0/RP0/CPU0:ucsd-data-sw#"""

CERN_24152 = """

Sun Sep 27 05:25:46.895 UTC
Local  Outgoing    Prefix             Outgoing     Next Hop        Bytes
Label  Label       or ID              Interface                    Switched
------ ----------- ------------------ ------------ --------------- ------------
24152  24167       EVPN:9019                       10.134.128.1    0
RP/0/RP0/CPU0:cern-data-sw#"""

UCSD_24167 = """

Sun Sep 27 05:25:47.380 UTC
Local  Outgoing    Prefix             Outgoing     Next Hop        Bytes
Label  Label       or ID              Interface                    Switched
------ ----------- ------------------ ------------ --------------- ------------
24167  Pop         PW(EVI=9019 AC-ID=864)   \\
                                      BE100.3149   point2point     0
RP/0/RP0/CPU0:ucsd-data-sw#"""

NO_ENTRY = """

Sun Sep 27 05:26:00.648 UTC
RP/0/RP0/CPU0:ucsd-data-sw#"""


def record():
    ends = [dict(device=CERN, ac="Hu0/0/0/6.864"), dict(device=UCSD, ac="BE100.3149")]
    return dict(name="circuit", service_type="l2ptp", devices=[CERN, UCSD], system_status="up", status="up",
                in_sync=None, dataplane_status="not_checked", device_sync={CERN: "in-sync", UCSD: "in-sync"},
                live_l2={"endpoints": [dict(end, st="UP", group="evpn_vpws", xconnect="evpn_vpws_9001",
                                            ac_st="UP", seg2_st="UP") for end in ends]})


def saved(**changed):
    """The saved outputs by (device, command); ``cern``/``ucsd``/``lookup`` replace one of them."""
    out = {(CERN, DETAIL): CERN_DETAIL, (UCSD, DETAIL): UCSD_DETAIL, (CERN, LOOKUP): CERN_24152}
    for key, text in changed.items():
        out[{"cern": (CERN, DETAIL), "ucsd": (UCSD, DETAIL), "lookup": (CERN, LOOKUP)}[key]] = text
    return out


async def run(outputs, rec=None):
    calls = []

    async def call(client, tool, args):
        assert tool == "exec_show"
        key = (args["device_name"], args["input_command"])
        calls.append(key)
        return {"status": "success", "data": {"result": outputs.get(key, "% Invalid input detected")}}

    return await evaluate_with_labels(rec or record(), Probe(None, call, {})), calls


def paths(result):
    return {c["observation"].split(":")[0]: c["status"] for c in result["checks"] if c["check"] == "label_path"}


def test_reads_this_circuits_labels_from_the_xconnect_detail():
    assert xc_labels(CERN_DETAIL, "Hu0/0/0/6.864") == {
        "evi": "9019", "local": "24005", "remote": "24152", "ac_id": ("864", "3149")}
    assert xc_labels(UCSD_DETAIL, "BE100.3149") == {
        "evi": "9019", "local": "24167", "remote": "24005", "ac_id": ("3149", "864")}
    # Output for another attachment, two circuits at once, or no output is not this circuit's evidence.
    assert xc_labels(CERN_DETAIL, "Hu0/0/0/7.864") is None
    assert xc_labels(CERN_DETAIL + UCSD_DETAIL, "Hu0/0/0/6.864") is None
    assert xc_labels(NO_ENTRY, "Hu0/0/0/6.864") is None


def test_reads_what_the_sender_puts_on_the_wire_for_a_label():
    assert forwarded(CERN_24152, "24152") == ("24167", "EVPN:9019")
    assert forwarded(UCSD_24167, "24167") == ("Pop", "PW(EVI=9019")
    assert forwarded(NO_ENTRY, "24005") is None
    assert forwarded(CERN_24152, "24153") is None


async def test_label_shown_for_the_far_end_is_followed_to_the_label_actually_sent():
    result, calls = await run(saved())

    assert (result["status"], result["needs_investigation"]) == ("up", False)
    assert paths(result) == {f"{CERN} -> {UCSD}": "pass", f"{UCSD} -> {CERN}": "pass"}
    # ucsd shows cern's own label, so only cern's label needs looking up.
    assert sorted(calls) == sorted([(CERN, DETAIL), (UCSD, DETAIL), (CERN, LOOKUP)])
    assert (result["tool_calls"], result["customer_delivery_tested"]) == (3, False)


async def test_sender_sending_a_label_the_receiver_does_not_hold_is_unknown_not_down():
    result, _ = await run(saved(lookup=CERN_24152.replace("24167", "24160")))

    assert (result["status"], result["needs_investigation"]) == ("unknown", True)
    assert paths(result) == {f"{CERN} -> {UCSD}": "unknown", f"{UCSD} -> {CERN}": "pass"}
    assert f"{CERN} -> {UCSD}: sender label 24152 is sent as 24160; the receiver's label is 24167" in result["reason"]


async def test_label_difference_that_cannot_be_followed_is_unknown():
    result, _ = await run(saved(lookup=NO_ENTRY))
    assert (result["status"], paths(result)[f"{CERN} -> {UCSD}"]) == ("unknown", "unknown")
    assert "forwarding entry was not read" in result["reason"]

    # An entry for the same number that belongs to another service is not this circuit's.
    result, _ = await run(saved(lookup=CERN_24152.replace("EVPN:9019", "EVPN:9020")))
    assert result["status"] == "unknown"

    result, calls = await run(saved(ucsd="% Invalid input detected"))
    assert result["status"] == "unknown" and paths(result) == {}
    assert (CERN, LOOKUP) not in calls


async def test_two_ends_must_describe_one_circuit():
    result, _ = await run(saved(ucsd=UCSD_DETAIL.replace("evi 9019", "evi 9020")))
    assert result["status"] == "unknown" and "one circuit" in result["reason"]

    result, _ = await run(saved(ucsd=UCSD_DETAIL.replace("AC ID        3149", "AC ID        3150")))
    assert result["status"] == "unknown" and "one circuit" in result["reason"]


@pytest.mark.parametrize("change, status", [({"st": "DN"}, "down"), ({"error": "ac_not_found"}, "unknown")])
async def test_state_fault_or_gap_is_reported_without_reading_labels(change, status):
    rec = record()
    rec["live_l2"]["endpoints"][0].update(change)

    result, calls = await run(saved(), rec)

    assert (result["status"], calls) == (status, [])


async def test_sync_prerequisite_reads_nothing():
    rec = record()
    rec["device_sync"][UCSD] = None

    result, calls = await run(saved(), rec)

    assert (result["status"], calls) == ("not_checked", [])


async def test_collector_follows_labels_for_circuits_whose_ends_are_up(monkeypatch):
    import nso_facts.service_collect as sc

    outputs = saved()
    outputs[CERN, "l2vpn xconnect"] = "evpn_vpws evpn_vpws_9001\n UP Hu0/0/0/6.864 UP EVPN 9019,3149,24152 UP"
    outputs[UCSD, "l2vpn xconnect"] = "evpn_vpws evpn_vpws_9001\n UP BE100.3149 UP EVPN 9019,864,10.143.0.1 UP"
    calls = []

    async def fake_call(client, tool, args=None):
        calls.append((args["device_name"], args["input_command"]))
        return {"status": "success", "data": {"result": outputs.get(calls[-1], "% Invalid input detected")}}

    monkeypatch.setattr(sc, "call_mcp", fake_call)
    circuit = {"name": "circuit",
               "stp-a": {"device": CERN, "interface": {"type": "HundredGigE", "id": "0/0/0/6", "outervlan": 864}},
               "stp-z": {"device": UCSD, "interface": {"type": "Bundle-Ether", "id": "100", "outervlan": 3149}}}

    async def collect():
        services = {"l2ptp": {"status": "success", "data": {"services": [circuit]}}}
        out = await sc.collect_service_health(None, services, {}, {CERN: "in-sync", UCSD: "in-sync"},
                                              service_sync_mode="skip", operational_policy="routine")
        return out["l2ptp/circuit"]

    service = await collect()
    assert service["basic_checks"]["status"] == service["operational_status"] == "up"
    assert paths(service["basic_checks"]) == {f"{CERN} -> {UCSD}": "pass", f"{UCSD} -> {CERN}": "pass"}
    assert len(calls) == 5 and (CERN, LOOKUP) in calls

    outputs[CERN, LOOKUP] = CERN_24152.replace("24167", "24160")
    service = await collect()
    assert service["basic_checks"]["status"] == service["status"] == "unknown"
    assert service["basic_checks"]["needs_investigation"]


async def test_check_time_is_not_earlier_than_the_reads_it_rests_on():
    result, _ = await run(saved())

    assert result["reason"] == "All service endpoint xconnects are UP; service label followed in each direction"
    assert all(source["collected_at"] <= result["checked_at"] for source in result["sources"])
    assert [s["command"] for s in result["sources"] if s["available"]] == [DETAIL, DETAIL, LOOKUP]


async def test_circuit_on_one_device_has_no_label_to_follow():
    rec = record()
    rec["devices"] = [CERN]
    rec["device_sync"] = {CERN: "in-sync"}
    rec["live_l2"]["endpoints"][1].update(device=CERN, ac="Hu0/0/0/7.864")

    result, calls = await run(saved(), rec)

    assert (result["status"], calls) == ("up", [])
    assert result["reason"] == "All service endpoint xconnects are UP"
