"""L3RT fixed check: a gateway address configured on two services of one device.

The route output is as amst-data-sw returned it in the scan of 6 October 2026,
when two pairs of services shared a gateway address and the LLM found it.
"""
import pytest

from diagnostic_mas.case import Budget, CaseFile, add_evidence
from diagnostic_mas.operational_checks.l3rt import gateway_owners
from diagnostic_mas.operational_checks.runner import evaluate
from test_operational_service_types import bridge_outputs, call_from, endpoint, record

PE = "amst-data-sw"
ROUTE_V4 = """

Tue Oct  6 22:36:24.773 UTC

Routing entry for 10.145.2.0/24
  Known via "connected", distance 0, metric 0 (connected)
  Installed Oct  5 17:05:07.020 for 1d05h
  Routing Descriptor Blocks
    directly connected, via BVI4221
      Route metric is 0
  No advertising protos.
RP/0/RP0/CPU0:amst-data-sw#"""
CEF_V4 = """
10.145.2.0/24, version 43707916, attached, connected, glean adjacency, internal 0x3000061 0x0 (ptr 0xa021e2d8) [1], 0x400 (0x93e81db8), 0x0 (0x0)
   via BVI4221, 2 dependencies, weight 0, class 0 [flags 0x8]
     glean adjacency
    Hash  OK  Interface                 Address
    0     Y   BVI4221                   glean
RP/0/RP0/CPU0:amst-data-sw#"""
GATEWAY = {"address": "10.145.2.1", "netmask": 24}
SHARED = {"gateway_owners": {(PE, "10.145.2.1"): ["net3", "v4h"]}}


def service(name: str) -> dict:
    return endpoint(PE) | {"name": name, "gateway-ipv4": dict(GATEWAY)}


def outputs(bvi: str, *, route: str = ROUTE_V4) -> dict:
    """What the device returns for a service whose own gateway interface is ``bvi``."""
    out = {(PE, k): v for k, v in bridge_outputs(bvi=bvi).items()}
    out[PE, f"interfaces {bvi}"] = f"{bvi} is up, line protocol is up"
    out[PE, f"running-config interface {bvi}"] = f"interface {bvi}\n ipv4 address 10.145.2.1 255.255.255.0\n!\n"
    out[PE, "route 10.145.2.0/24"] = route
    out[PE, "cef 10.145.2.0/24"] = CEF_V4
    return out


async def check(name: str, bvi: str, *, fleet=SHARED, route: str = ROUTE_V4) -> dict:
    return await evaluate(record("l3rt", [PE]), service(name), None, call_from(outputs(bvi, route=route), []), {},
                          fleet=fleet)


def by_check(result: dict) -> dict:
    return {c["check"]: (c["status"], c["observation"]) for c in result["checks"]}


def test_lists_gateway_addresses_configured_on_two_services_of_one_device():
    other_device = endpoint("fiu-data-sw") | {"name": "elsewhere", "gateway-ipv4": dict(GATEWAY)}
    v6_a = endpoint(PE) | {"name": "v6m", "gateway-ipv6": {"address": "2602:fcfb:1f:1::1", "netmask": 64}}
    v6_b = endpoint(PE) | {"name": "meas", "gateway-ipv6": {"address": "2602:FCFB:1F:1:0:0:0:1", "netmask": 64}}
    alone = endpoint(PE) | {"name": "alone", "gateway-ipv4": {"address": "10.145.9.1", "netmask": 24}}
    broken = endpoint(PE) | {"name": "broken", "gateway-ipv4": {"address": "not-an-address"}}

    assert gateway_owners([service("v4h"), service("net3"), other_device, v6_a, v6_b, alone, broken]) == {
        (PE, "10.145.2.1"): ["net3", "v4h"], (PE, "2602:fcfb:1f:1::1"): ["meas", "v6m"]}


async def test_service_whose_subnet_route_is_bound_to_the_other_services_interface_is_down_with_no_investigation():
    result = await check("net3", "BVI4274")

    assert (result["status"], result["needs_investigation"]) == ("down", False)
    assert by_check(result)["gateway_unique"] == (
        "fault", "10.145.2.1 is also configured on service v4h; 10.145.2.0/24 is bound to BVI4221, "
                 "not this service's BVI4274")
    assert "gateway_route_binding" not in by_check(result)
    assert result["reason"].startswith("10.145.2.1 is also configured on service v4h")
    assert not result["customer_delivery_tested"]


async def test_service_that_holds_the_shared_address_stays_up_and_says_who_shares_it():
    result = await check("v4h", "BVI4221")

    assert (result["status"], result["needs_investigation"]) == ("up", False)
    assert by_check(result)["gateway_route_binding"][0] == "pass"
    assert by_check(result)["gateway_shared"] == (
        "pass", "10.145.2.1 is also configured on service net3; 10.145.2.0/24 is bound to this service's BVI4221")


async def test_shared_address_alone_is_not_a_fault_without_the_devices_route():
    result = await check("net3", "BVI4274", route="% Network not in table")

    assert (result["status"], result["needs_investigation"]) == ("unknown", True)
    assert "gateway_unique" not in by_check(result)


async def test_route_bound_elsewhere_stays_unknown_when_no_other_service_is_configured_with_the_address():
    result = await check("net3", "BVI4274", fleet={"gateway_owners": {}})

    assert (result["status"], result["needs_investigation"]) == ("unknown", True)
    assert by_check(result)["gateway_route_binding"][0] == "unknown"
    assert (await check("net3", "BVI4274", fleet=None))["status"] == "unknown"


async def test_collector_finds_the_pair_and_only_the_losing_service_is_down(monkeypatch):
    import nso_facts.service_collect as sc
    from diagnostic_mas.dataplane_verify import init_service_coverage, select_dataplane_candidates
    from diagnostic_mas.operator_report import _coverage_label
    from diagnostic_mas.service_final_status import final_service_assessments

    # One device fixture serves every service here, so only the losing side is checked.
    replies = outputs("BVI4274")

    async def fake_call(client, tool, args=None):
        key = (args["device_name"], args["input_command"])
        return {"status": "success", "data": {"result": replies.get(key, "% Invalid input detected")}}

    monkeypatch.setattr(sc, "call_mcp", fake_call)
    services = {"l3rt": {"status": "success", "data": {"services": [service("net3"), service("other-site")]}}}
    services["l3rt"]["data"]["services"][1]["device"] = "fiu-data-sw"
    services["l3rt"]["data"]["services"].append(service("v4h"))
    out = await sc.collect_service_health(None, services, {}, {PE: "in-sync", "fiu-data-sw": "in-sync"},
                                          service_sync_mode="skip", operational_policy="routine")

    loser = out["l3rt/net3"]
    assert (loser["basic_checks"]["status"], loser["status"], loser["basic_checks"]["needs_investigation"]) == (
        "down", "down", False)
    assert "is also configured on service v4h" in loser["basic_checks"]["reason"]

    case = CaseFile(budget=Budget(0, 0))
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services", "payload": {"extra": {"services": out}}})
    init_service_coverage(case)
    assert case.service_coverage["net3"] == "fixed_check_decided"
    assert _coverage_label("fixed_check_decided") == "Decided by the fixed check (no investigation needed)"
    assert "net3" not in {rec["name"] for _ev, rec in select_dataplane_candidates(case)}
    final = {name: (status, reason) for _kind, name, _svc, status, reason in final_service_assessments(case)}
    assert final["net3"] == ("down", "Down — decided by the fixed check")


def _decided_case() -> CaseFile:
    """A scan holding one service the fixed check decided Down, and its service issue."""
    from diagnostic_mas.case import open_issue

    reason = "10.145.2.1 is also configured on service v4h; 10.145.2.0/24 is bound to BVI4221, not this service's BVI4274"
    rec = record("l3rt", [PE]) | {
        "name": "net3", "status": "down", "operational_status": "down", "dataplane_status": "not_checked",
        "basic_checks": {"schema_version": 1, "status": "down", "reason": reason, "needs_investigation": False,
                         "sync_ready": True, "checks": [{"check": "gateway_unique", "status": "fault", "device": PE,
                                                         "observation": reason, "conclusive": True}]}}
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = [PE]
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services",
                        "payload": {"extra": {"services": {"l3rt/net3": rec}}}})
    open_issue(case, code="service_down", severity="high", layer="services", edge_id="net3", devices=[PE],
               message="l3rt net3: down", evidence_ids=[])
    return case


def test_service_decided_by_the_fixed_check_is_not_sent_to_the_follow_up_investigation_either():
    from diagnostic_mas.dataplane_verify import init_service_coverage
    from diagnostic_mas.drill import select_drill_issues

    case = _decided_case()
    assert [i["edge_id"] for i in select_drill_issues(case, limit=2)] == ["net3"]     # before coverage is known

    init_service_coverage(case)

    assert select_drill_issues(case, limit=2) == []


def test_report_gives_the_cause_and_leaves_the_change_to_a_person():
    from diagnostic_mas.dataplane_verify import init_service_coverage
    from diagnostic_mas.report import render_report

    case = _decided_case()
    init_service_coverage(case)

    report = render_report(case, summary="s", duration_seconds=1, dry_run=True, llm_model="m")

    assert "**Coverage:** Decided by the fixed check (no investigation needed)" in report
    assert ("**Next action:** The fixed check's finding above is the cause. Resolving it is a change for a person "
            "to decide and approve; no LLM investigation is needed.") in report
    assert "LLM investigation was not performed" not in report
