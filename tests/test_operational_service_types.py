"""Offline proofs for deterministic service checks: no live NSO or LLM."""
import pytest
from diagnostic_mas.operational_checks.runner import evaluate
from diagnostic_mas.operational_checks.probe import Probe, cef_ready
from diagnostic_mas.operational_checks.l2sts import evpn, replication
from nso_facts.service_collect import collect_service_health


def endpoint(device, port="0/0/0/1"):
    return {"device": device, "interface": [{"type": "HundredGigE", "id": port, "outervlan": 100}]}


def record(kind, devices):
    return dict(name="test", service_type=kind, devices=devices, in_sync=None,
                system_status="up", device_sync={d: "in-sync" for d in devices})


def bridge_outputs(evi=None, bvi=None):
    ac = "Hu0/0/0/1.100"
    header = "Bridge group: bg-exact, bridge-domain: bd-short-, id: 0, state: up"
    detail = header + "\n  AC: HundredGigE0/0/0/1.100, state is up\n    Type VLAN; Num Ranges: 1\n    VLAN ranges: [100, 100]\n"
    if evi:
        detail += f" EVI: {evi} (up)\n"
    if bvi:
        detail += f" AC: {bvi}, state is up\n"
    return {
        f"interfaces {ac}": f"{ac} is up, line protocol is up",
        f"l2vpn bridge-domain interface {ac}": header + "\nRP/0/RP0/CPU0:pe#",
        "l2vpn bridge-domain group bg-exact detail": detail,
        "l2vpn forwarding bridge-domain bg-exact:bd-short- detail location 0/RP0/CPU0":
            "Bridge-domain name: bg-exact:bd-short-, id: 0, state: up\n"
            " Broadcast & Multicast: enabled\n Unknown unicast: enabled\n"
            " HundredGigE0/0/0/1.100, state: oper up\n",
    }


def call_from(outputs, calls):
    async def call(client, tool, args):
        assert tool == "exec_show"
        key = (args["device_name"], args["input_command"])
        calls.append(key)
        return {"status": "success", "data": {"result": outputs.get(key, "% Invalid input")}}
    return call


@pytest.mark.asyncio
async def test_local_bridge_and_cache_and_unknown():
    outputs = {("a", k): v for k,v in bridge_outputs().items()}
    calls, cache = [], {}
    args = (record("l2bridge", ["a"]), endpoint("a"), None, call_from(outputs, calls), cache)
    result = await evaluate(*args)
    assert result["status"] == "up"
    assert not result["customer_delivery_tested"]
    assert len(calls) == 6
    assert (await evaluate(*args))["tool_calls"] == 0
    outputs[("a", "l2vpn forwarding bridge-domain bg-exact:bd-short- detail location 0/RP0/CPU0")] = "% Invalid input"
    result = await evaluate(*args[:-1], {})
    assert result["status"] == "unknown"
    assert result["sources"]


@pytest.mark.asyncio
async def test_bridge_wrong_identity_missing_member_and_confirmed_down():
    outputs = {("a", k): v for k,v in bridge_outputs().items()}
    key = ("a", "l2vpn bridge-domain group bg-exact detail")
    outputs[key] = outputs[key].replace("0/0/0/1.100", "0/0/0/9.100")
    assert (await evaluate(record("l2bridge", ["a"]), endpoint("a"), None, call_from(outputs, []), {}))["status"] == "unknown"
    outputs[("a", "interfaces Hu0/0/0/1.100")] = "Hu0/0/0/1.100 is down, line protocol is down"
    assert (await evaluate(record("l2bridge", ["a"]), endpoint("a"), None, call_from(outputs, []), {}))["status"] == "down"


def evi_text(peer, label, rt="398900:9001"):
    return ("9001 MPLS bd-short- EVPN\n"
            f" Multicast Label: {label}\n RD Auto : (auto) {peer}:9001\n"
            f" Route Targets in Use Type\n {rt} Import\n {rt} Export\n")


def rep_text(peer, label):
    return f"9001 MPLS 0 {peer}\n PMSI Type: 6\n Nexthop: {peer}\n Label: {label}\n Source: Remote\n"


def sts_outputs():
    out = {}
    for d, peer, label, remote, remote_label in [("a", "10.0.0.1", 100, "10.0.0.2", 200), ("b", "10.0.0.2", 200, "10.0.0.1", 100)]:
        out.update({(d,k):v for k,v in bridge_outputs(evi=9001).items()})
        out[d,"evpn evi vpn-id 9001 detail"] = evi_text(peer, label)
        out[d,"evpn evi vpn-id 9001 inclusive-multicast detail"] = rep_text(remote, remote_label)
        out[d,f"cef {remote}/32"] = f"{remote}/32, version 1\n via 10.1.1.1/32, HundredGigE0/0/0/2\n labels imposed {{16001}}"
    return out


@pytest.mark.asyncio
async def test_sts_effective_rts_and_both_direction_replication():
    inst = {"site-a": endpoint("a"), "site-z": endpoint("b")}
    r = record("l2sts", ["a", "b"])
    outputs = sts_outputs()
    assert (await evaluate(r, inst, None, call_from(outputs, []), {}))["status"] == "up"
    outputs["b","evpn evi vpn-id 9001 inclusive-multicast detail"] = "no entries"
    assert (await evaluate(r, inst, None, call_from(outputs, []), {}))["status"] == "unknown"
    outputs["b","evpn evi vpn-id 9001 detail"] = evi_text("10.0.0.2", 200, "398900:9999")
    result = await evaluate(r, inst, None, call_from(outputs, []), {})
    assert result["status"] == "down"
    assert any(c["check"] == "rt_compatibility" and c["status"] == "fault" for c in result["checks"])


def test_no_guessed_auto_rt_or_tep_only_proof():
    assert evpn("9001 MPLS bd-short- EVPN\n RT Auto: 398900:9001", "9001", "bd-short-")["imports"] == set()
    assert evpn(evi_text("10.0.0.1", 100), "9002", "bd-short-") is None
    assert not replication(rep_text("10.0.0.2", 200), "9001", "10.0.0.2", 300)
    assert not cef_ready("10.0.0.2/32, version 1\n TEPid: 0x02000009", "10.0.0.2/32", labeled=True)


def routed_outputs():
    outputs = {("a",k):v for k,v in bridge_outputs(bvi="BVI50000").items()}
    outputs["a","interfaces BVI50000"] = "BVI50000 is up, line protocol is up"
    outputs["a","running-config interface BVI50000"] = "interface BVI50000\n ipv4 address 192.0.2.1 255.255.255.0\n!\n"
    outputs["a","route 192.0.2.0/24"] = 'Routing entry for 192.0.2.0/24\n Known via "connected"\n directly connected, via BVI50000'
    outputs["a","cef 192.0.2.0/24"] = "192.0.2.0/24, version 1\n attached to BVI50000"
    return outputs


@pytest.mark.asyncio
async def test_l3_gateway_family_and_border_coverage():
    inst = endpoint("a") | {"gateway-ipv4": {"address": "192.0.2.1", "netmask": 24}}
    r = record("l3rt", ["a"])
    outputs = routed_outputs()
    assert (await evaluate(r, inst, None, call_from(outputs, []), {}))["status"] == "up"
    inst["gateway-ipv6"] = {"address": "2001:db8::1", "netmask": 64}
    assert (await evaluate(r, inst, None, call_from(outputs, []), {}))["status"] == "unknown"
    del inst["gateway-ipv6"]
    inst["external-access"] = {"ip-type": "v4", "border-router": [{"device": "border"}]}
    r = record("l3rt", ["a", "border"])
    calls = []
    result = await evaluate(r, inst, None, call_from(outputs, calls), {})
    assert result["status"] == "unknown"
    assert any(d == "border" and "route " in cmd for d,cmd in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["l2bridge", "l2sts", "l3rt"])
async def test_sync_gate_no_calls(kind):
    r = record(kind, ["a"])
    r["device_sync"]["a"] = "out-of-sync"
    calls = []
    result = await evaluate(r, endpoint("a"), None, call_from({}, calls), {})
    assert result["status"] == "not_checked" and calls == []


@pytest.mark.asyncio
async def test_collection_sts_does_not_probe_xconnect(monkeypatch):
    import nso_facts.service_collect as sc
    calls = []
    monkeypatch.setattr(sc, "call_mcp", call_from({}, calls))
    instance = {"name": "s", "site-a": endpoint("a"), "site-z": endpoint("b")}
    services = {"l2sts": {"status": "success", "data": {"services": [instance]}}}
    result = await collect_service_health(None, services, {}, {"a": "in-sync", "b": "in-sync"}, service_sync_mode="skip", operational_policy="routine")
    assert not any("xconnect" in cmd for _,cmd in calls)
    assert result["l2sts/s"]["basic_checks"]["status"] == "unknown"
    assert result["l2sts/s"]["dataplane_status"] == "not_checked"


def test_saved_xr_outputs_replay():
    import json
    from pathlib import Path
    from diagnostic_mas.operational_checks.probe import bridge_identity, member_state
    data = json.loads((Path(__file__).parent / "fixtures/operational_xr_outputs.json").read_text())
    assert bridge_identity(data["bridge"]) == ("bg-L2STS_MAX_WASH-be3e587a-3ba6", "bd-L2STS_MAX_WASH-be3e587a-")
    assert member_state(data["forwarding"], "Hu0/0/0/7.2084", True) == "pass"
    view = evpn(data["evi"], "9011", "bd-L2STS_MAX_WASH-be3e587a-")
    assert view["imports"] == view["exports"] == {"398900:9011"}
    assert replication(data["replication"], "9011", "10.130.0.1", 31687)


@pytest.mark.asyncio
async def test_tool_budget_is_bounded():
    calls = []
    probe = Probe(None, call_from({}, calls), {}, max_calls=2)
    for i in range(5):
        await probe.show("a", f"interfaces Hu0/0/0/{i}")
    assert len(calls) == 2
    assert any(c["check"] == "collection_budget" for c in probe.checks)


@pytest.mark.asyncio
async def test_wrong_encapsulation_is_not_a_basic_pass():
    outputs = {("a",k):v.replace("[100, 100]", "[200, 200]") for k,v in bridge_outputs().items()}
    result = await evaluate(record("l2bridge", ["a"]), endpoint("a"), None, call_from(outputs, []), {})
    assert result["status"] == "unknown"
    assert any(c["check"] == "encapsulation" and c["status"] == "unknown" for c in result["checks"])


@pytest.mark.asyncio
async def test_ipv6_gateway_pass_and_wrong_local_route_binding():
    inst = endpoint("a") | {"gateway-ipv6": {"address": "2001:db8::1", "netmask": 64}}
    outputs = routed_outputs()
    outputs["a","running-config interface BVI50000"] = "interface BVI50000\n ipv6 address 2001:db8::1/64\n!\n"
    outputs["a","route ipv6 2001:db8::/64"] = 'Routing entry for 2001:db8::/64\n Known via "connected"\n directly connected, via BVI50000'
    outputs["a","cef ipv6 2001:db8::/64"] = "2001:db8::/64, version 1\n attached to BVI50000"
    r = record("l3rt", ["a"])
    assert (await evaluate(r, inst, None, call_from(outputs, []), {}))["status"] == "up"
    outputs["a","route ipv6 2001:db8::/64"] = 'Routing entry for 2001:db8::/64\n Known via "connected"\n directly connected, via BVI999'
    assert (await evaluate(r, inst, None, call_from(outputs, []), {}))["status"] == "unknown"


@pytest.mark.asyncio
async def test_bulk_snapshots_reused_across_services():
    outputs = {("a", k):v for k,v in bridge_outputs().items()}
    outputs["a", "interfaces brief"] = "Hu0/0/0/1.100 up up ARPA\n"
    outputs["a", "l2vpn bridge-domain detail"] = outputs["a", "l2vpn bridge-domain group bg-exact detail"] + "\nRP/0/RP0/CPU0:a#"
    calls, cache = [], {}
    for name in ["first", "second", "third"]:
        r = record("l2bridge", ["a"]) | {"name": name}
        assert (await evaluate(r, endpoint("a"), None, call_from(outputs,calls),cache))["status"] == "up"
    assert len(calls) == 3  # one interface snapshot, one BD snapshot, one forwarding view
    assert not any(cmd.startswith("interfaces Hu") or "group bg" in cmd for _,cmd in calls)


@pytest.mark.asyncio
async def test_routed_access_split_horizon_is_not_bridging_fault():
    inst = endpoint("a") | {"gateway-ipv4": {"address": "192.0.2.1", "netmask": 24}}
    outputs = routed_outputs()
    outputs["a", "l2vpn bridge-domain group bg-exact detail"] += " Split Horizon Group: Access\n"
    result = await evaluate(record("l3rt", ["a"]), inst, None, call_from(outputs, []), {})
    assert result["status"] == "up"
    # The same restriction is still significant for pure local switching.
    r = await evaluate(record("l2bridge", ["a"]), endpoint("a"), None, call_from(outputs, []), {})
    assert r["status"] == "unknown"


@pytest.mark.asyncio
async def test_host_lookup_is_lpm_and_queries_every_border():
    inst = endpoint("a") | {"external-access": {"ip-type": "v4", "permit-ipv4": [{"address": "192.0.2.82"}], "border-router": [{"device": "b"}, {"device": "c"}]}}
    outputs = routed_outputs()
    for dev in ["a", "b", "c"]:
        outputs[dev, "route 192.0.2.82"] = outputs["a", "route 192.0.2.0/24"] if dev == "a" else 'Routing entry for 192.0.2.0/24\n Known via "bgp"'
        outputs[dev, "cef 192.0.2.0/24"] = outputs["a", "cef 192.0.2.0/24"] if dev == "a" else "192.0.2.0/24, version 1\n 0 Y Hu0/1 10.0.0.1"
        outputs[dev, "route 0.0.0.0/0"] = 'Routing entry for 0.0.0.0/0\n Known via "bgp"'
        outputs[dev, "cef 0.0.0.0/0"] = "0.0.0.0/0, version 1\n 0 Y Hu0/1 10.0.0.1"
    calls=[]
    result = await evaluate(record("l3rt",["a","b","c"]),inst,None,call_from(outputs,calls),{})
    assert result["status"] == "up"
    assert all((d,"route 192.0.2.82") in calls for d in ["a","b","c"])
    assert not any(cmd.endswith("192.0.2.82/32") for _,cmd in calls)


def test_nso40_implicit_null_replay():
    import json
    from pathlib import Path
    data = json.loads((Path(__file__).parent / "fixtures/nso40_operational_regressions.json").read_text())
    text = data["implicit_null_cef"].replace("\r", "")
    assert cef_ready(text, "10.130.0.1/32", labeled=True)
    assert not cef_ready(text.replace("local adjacency", "unresolved"), "10.130.0.1/32", labeled=True)
    assert "Split Horizon Group: Access" in data["routed_bridge"]
    assert "Network not in table" in data["exact_host_route"]
