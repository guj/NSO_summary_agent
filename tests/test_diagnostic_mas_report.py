from __future__ import annotations

import re

from diagnostic_mas.case import Budget, CaseFile, add_diagnosis, add_evidence, open_issue
from diagnostic_mas.operator_report import (
    format_duration,
    format_followup_operator,
    format_result_line,
)
from diagnostic_mas.report import (
    dedupe_evidence,
    format_services_table,
    render_report,
)
from diagnostic_mas.roles.summary import scrub_internal_ids
from diagnostic_mas.state_paths import diagnostic_mas_state_dir


def test_services_footnote_systemup_omitted_excludes_displayed_digs():
    """SystemUp omitted must be total SystemUp minus shown dig instances."""
    from diagnostic_mas.operator_report import format_services_operator

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    services = {}
    for i in range(5):
        name = f"svc-{i}"
        services[f"l2sts/{name}"] = {
            "name": name,
            "service_type": "l2sts",
            "status": "up",
            "system_status": "up",
            "dataplane_status": "not_checked",
            "devices": ["wash-data-sw", "max-data-sw"],
            "basic_checks": {"status": "up", "sync_ready": True},
        }
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {"extra": {"services": services}},
        },
    )
    for name in ("svc-0", "svc-1"):
        add_diagnosis(
            case,
            kind="dataplane",
            source="llm",
            status="unknown",
            subject={"name": name, "service_type": "l2sts"},
            observed="gap",
            cause="incomplete",
            extra={"complete": False},
        )
    text = "\n".join(format_services_operator(case, services_detail=False))
    assert "3 services skipped LLM investigation" in text
    assert "5 services skipped LLM investigation" not in text


def test_scrub_internal_ids_from_remedies():
    raw = (
        "1) Check optics on renc (Grounded in is_1, ev_1, ev_4)\n"
        "2) Verify BGP on edge is_2 and ho_3"
    )
    cleaned = scrub_internal_ids(raw)
    assert "is_1" not in cleaned
    assert "ev_1" not in cleaned
    assert "ev_4" not in cleaned
    assert "ho_3" not in cleaned
    assert "Check optics on renc" in cleaned


def _case_with_dropped_devices(monkeypatch, services: dict, dropped: dict[str, str]) -> CaseFile:
    """A case whose device issues are written the way a real run writes them."""
    from diagnostic_mas.ingest import ingest_quarantined_devices

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {"extra": {"services": services}},
        },
    )
    monkeypatch.setattr("nso_facts.mcp_client.quarantined_devices", lambda: dropped)
    ingest_quarantined_devices(case)
    return case


def test_result_line_counts_unknown_services_on_a_dropped_device_whose_sync_succeeded(
    monkeypatch,
):
    from diagnostic_mas.operator_report import format_result_line

    def service(name: str, devices: list[str], **fields) -> dict:
        return {"name": name, "service_type": "l3rt", "devices": devices,
                "device_sync": {d: "in-sync" for d in devices}, **fields}

    # Sync was verified before the devices were dropped; later queries were skipped.
    services = {
        "l3rt/a": service("a", ["gpn-data-sw"], status="unknown"),
        "l3rt/b": service("b", ["gpn-data-sw", "kans-data-sw"], status="unknown",
                          operational_status="unknown"),
        "l3rt/c": service("c", ["kans-data-sw"], status="up", operational_status="up"),
    }
    timeout = ("exec_show (interfaces BVI6203): Request timed out after 30s: "
               "https://192.0.2.1:443/restconf/data/tailf-ncs:devices")
    case = _case_with_dropped_devices(
        monkeypatch, services,
        {"gpn-data-sw": timeout, "kans-data-sw": timeout, "scm-data-sw": timeout},
    )

    result = format_result_line(case)

    assert "1 OpUp, 0 Down, 0 Degraded, 2 Unknown" in result
    assert "`gpn-data-sw` (2 unknown services)" in result
    assert "`kans-data-sw` (1 unknown service)" in result
    assert "`scm-data-sw` (0 unknown services)" in result


def test_result_line_names_the_failed_operation_for_a_single_dropped_device(monkeypatch):
    from diagnostic_mas.operator_report import format_result_line

    refused = (
        "check_isis_adjacencies: RESTCONF 500 at https://192.0.2.1:443/restconf/data/x: "
        "Failed to connect to device scm-data-sw: connection refused: NEDCOM CONNECT: "
        "Connect timed out in new state"
    )
    case = _case_with_dropped_devices(monkeypatch, {}, {"scm-data-sw": refused})

    result = format_result_line(case)

    assert "1 device hit automated check_isis_adjacencies timeout: `scm-data-sw`" in result
    assert "automated scm-data-sw timeout" not in result


def test_result_line_states_service_status_before_dropped_devices(monkeypatch):
    from diagnostic_mas.operator_report import format_result_line

    services = {
        "l3rt/a": {"name": "a", "service_type": "l3rt", "devices": ["gpn-data-sw"],
                   "device_sync": {"gpn-data-sw": "in-sync"}, "status": "up",
                   "operational_status": "up"},
    }
    timeout = "check_isis_adjacencies: Request timed out after 30s: https://192.0.2.1:443/x"
    case = _case_with_dropped_devices(monkeypatch, services, {"scm-data-sw": timeout})

    result = format_result_line(case)

    assert result.startswith("Final service status: 1 OpUp, 0 Down, 0 Degraded, 0 Unknown.")
    assert "1 device hit automated check_isis_adjacencies timeout" in result


def _faulted_case(
    services: dict[str, tuple[str, list[str]]], *, failed_check_on: str | None = None
) -> CaseFile:
    """Services (name -> type, endpoints), each with a completed dataplane-down finding."""
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    records = {
        f"{kind}/{name}": {"name": name, "service_type": kind, "devices": devices}
        for name, (kind, devices) in services.items()
    }
    if failed_check_on:
        for record in records.values():
            record["basic_checks"] = {"status": "down", "checks": [
                {"check": "attachment", "device": device, "observation": "Hu0/0/0/1.0",
                 "status": "fault" if device == failed_check_on else "pass"}
                for device in record["devices"]
            ]}
    add_evidence(
        case,
        {"kind": "spine", "role": "service", "layer": "services",
         "payload": {"extra": {"services": records}}},
    )
    for name, (kind, _devices) in services.items():
        add_diagnosis(
            case, kind="dataplane", source="llm", status="down",
            subject={"name": name, "service_type": kind},
            observed="attachment circuit down", cause="no receive light",
        )
    return case


def test_follow_up_groups_faulted_services_sharing_type_and_endpoints():
    case = _faulted_case({
        "b1": ("l2bridge", ["mich-data-sw"]),
        "s1": ("l2sts", ["rutg-data-sw", "fiu-data-sw"]),
        "b2": ("l2bridge", ["mich-data-sw"]),
        "b3": ("l2bridge", ["mich-data-sw"]),
        "s2": ("l2sts", ["rutg-data-sw", "fiu-data-sw"]),
    })

    items = [line for line in format_followup_operator(case) if "Prioritize dataplane" in line]

    assert len(items) == 2
    assert "Prioritize dataplane down on 3 l2bridge services on `mich-data-sw`" in items[0]
    assert items[0].endswith("Services: `b1`, `b2`, `b3`.")
    assert "Prioritize dataplane down on 2 l2sts services on `fiu-data-sw` + `rutg-data-sw`" in items[1]
    assert items[1].endswith("Services: `s1`, `s2`.")


def test_follow_up_groups_by_the_device_whose_basic_check_failed():
    case = _faulted_case(
        {
            "s1": ("l2sts", ["rutg-data-sw", "fiu-data-sw"]),
            "s2": ("l2sts", ["mass-data-sw", "fiu-data-sw"]),
        },
        failed_check_on="fiu-data-sw",
    )

    items = [line for line in format_followup_operator(case) if "Prioritize dataplane" in line]

    assert len(items) == 1
    assert (
        "Prioritize dataplane down on 2 l2sts services with failed basic checks on "
        "`fiu-data-sw`"
    ) in items[0]
    assert items[0].endswith("Services: `s1`, `s2`.")


def _service_section(*, fix: str, observation) -> str:
    """Rendered section of one faulted service with a single basic check."""
    from diagnostic_mas.operator_report import format_services_operator

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    record = {
        "name": "s1", "service_type": "l2sts", "devices": ["fiu-data-sw"],
        "basic_checks": {"status": "down", "checks": [
            {"check": "effective_evpn", "device": "fiu-data-sw", "status": "pass",
             "observation": observation},
        ]},
    }
    add_evidence(
        case,
        {"kind": "spine", "role": "service", "layer": "services",
         "payload": {"extra": {"services": {"l2sts/s1": record}}}},
    )
    add_diagnosis(
        case, kind="dataplane", source="llm", status="down",
        subject={"name": "s1", "service_type": "l2sts"},
        observed="attachment circuit down", cause="no receive light", fix_suggestion=fix,
    )
    return "\n".join(format_services_operator(case, services_detail=False))


def test_collector_detail_prints_a_structured_observation_as_plain_pairs():
    text = _service_section(
        fix="Inspect the optic.",
        observation={"imports": ["398900:9001"], "exports": ["398900:9001"],
                     "peer": "10.141.0.1", "label": 24120, "evi": "9001"},
    )

    assert (
        "Basic check effective_evpn (pass): imports=398900:9001; exports=398900:9001; "
        "peer=10.141.0.1; label=24120; evi=9001"
    ) in text
    assert "{'" not in text


def test_collector_detail_keeps_a_plain_text_observation_as_it_is():
    text = _service_section(fix="Inspect the optic.", observation="Hu0/0/0/24/0.0")

    assert "Basic check effective_evpn (pass): Hu0/0/0/24/0.0" in text


def test_next_action_adds_the_approval_reminder_when_the_text_lacks_one():
    text = _service_section(fix="Inspect the optic on Hu0/0/0/24/0.", observation="x")

    assert (
        "**Next action:** Inspect the optic on Hu0/0/0/24/0. "
        "(human must approve any config change)."
    ) in text


def test_next_action_does_not_repeat_an_approval_reminder_already_in_the_text():
    fix = "Re-address one gateway; remediation requires human approval."

    text = _service_section(fix=fix, observation="x")

    assert f"**Next action:** {fix}\n" in text + "\n"
    assert "(human must approve any config change)" not in text


def _inconclusive_case(name: str, next_check: str) -> CaseFile:
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_evidence(
        case,
        {"kind": "spine", "role": "service", "layer": "services",
         "payload": {"extra": {"services": {
             f"l2bridge/{name}": {"name": name, "service_type": "l2bridge",
                                  "devices": ["cien-data-sw"]}}}}},
    )
    add_diagnosis(
        case, kind="dataplane", source="llm", status="unknown",
        subject={"name": name, "service_type": "l2bridge"},
        observed="operational access failed", cause="exec unavailable",
        extra={"complete": False, "verification_gap": {"next_check": next_check}},
    )
    return case


def test_follow_up_for_an_inconclusive_investigation_names_its_service():
    case = _inconclusive_case("dp-84934141", "After restoring access, read the state of this FD.")

    text = "\n".join(format_followup_operator(case))

    assert "For `dp-84934141`: After restoring access, read the state of this FD." in text


def test_follow_up_does_not_name_the_service_twice():
    case = _inconclusive_case("dp-84934141", "Re-read the FD for `dp-84934141` on port 14.")

    text = "\n".join(format_followup_operator(case))

    assert "Re-read the FD for `dp-84934141` on port 14." in text
    assert "For `dp-84934141`:" not in text


def test_follow_up_names_a_lone_faulted_service_directly():
    case = _faulted_case({
        "b1": ("l2bridge", ["mich-data-sw"]),
        "r1": ("l3rt", ["mich-data-sw"]),
    })

    text = "\n".join(format_followup_operator(case))

    assert "Prioritize dataplane down on `b1`: review the service evidence" in text
    assert "Prioritize dataplane down on `r1`: review the service evidence" in text


def test_result_line_timeout_quarantine_lists_operation_and_counts():
    from diagnostic_mas.operator_report import format_result_line

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    services = {
        "l3rt/a": {
            "name": "a",
            "service_type": "l3rt",
            "status": "unknown",
            "system_status": "unknown",
            "device_sync": {
                "star-data-sw": "error: Read timed out. (read timeout=10)"
            },
        },
        "l3rt/b": {
            "name": "b",
            "service_type": "l3rt",
            "status": "unknown",
            "system_status": "unknown",
            "device_sync": {
                "star-data-sw": "error: Read timed out. (read timeout=10)",
                "wash-data-sw": "error: Read timed out. (read timeout=10)",
            },
        },
    }
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {"extra": {"services": services}},
        },
    )
    for dev in ("scm-data-sw", "star-data-sw", "ucsd-data-sw", "wash-data-sw"):
        open_issue(
            case,
            code="device_live_unreachable",
            message=(
                f"exec_show (isis adjacency): "
                f"HTTPSConnectionPool(host='192.0.2.2', port=443): "
                f"Read timed out. (read timeout=10)"
            ),
            evidence_ids=[],
            devices=[dev],
        )
    result = format_result_line(case)
    assert "4 devices hit automated exec_show (isis adjacency) timeout, read timeout=10s" in result
    assert "`star-data-sw` (2 unknown services)" in result
    assert "`wash-data-sw` (1 unknown service)" in result
    assert "`scm-data-sw` (0 unknown services)" in result
    assert "not proof devices are down" in result
    assert "manual NSO access may still succeed" in result

    follow = "\n".join(format_followup_operator(case))
    assert (
        "Investigate the repeated 10-second NSO API timeout during WASH "
        "IS-IS collection"
    ) in follow
    assert "compare the same operation through MCP and the NSO CLI" in follow

def test_report_scrubs_hypothesis_ids():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    case.hypotheses.append(
        {"id": "hy_1", "text": "fiber cut (Grounded in is_1, ev_2)", "issue_ids": []}
    )
    text = render_report(case)
    assert "is_1" not in text
    assert "ev_2" not in text
    assert "fiber cut" in text
    assert "Open hypotheses" in text


def test_report_includes_issue_messages_in_device_attention():
    case = CaseFile(
        budget=Budget(max_deep_checks=5, max_handoffs=5),
        device_names=["renc-data-sw"],
    )
    open_issue(
        case,
        code="unknown_neighbor_address",
        message="renc-data-sw 10.148.0.1: could not map",
        evidence_ids=[],
        layer="routing",
        devices=["renc-data-sw"],
    )
    text = render_report(case)
    assert "10.148.0.1" in text
    assert "### renc-data-sw" in text
    assert "**Attention:**" in text


def test_followup_omits_document_coverage_for_not_selected():
    from diagnostic_mas.operator_report import format_followup_operator

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {
                "extra": {
                    "services": {
                        "l2ptp/peer-a": {
                            "name": "peer-a",
                            "service_type": "l2ptp",
                            "system_status": "up",
                            "status": "up",
                        },
                        "l3rt/peer-b": {
                            "name": "peer-b",
                            "service_type": "l3rt",
                            "system_status": "up",
                            "status": "up",
                        },
                    }
                }
            },
        },
    )
    case.service_coverage = {
        "peer-a": "category_peer_skipped",
        "peer-b": "basic_passed",
    }
    text = "\n".join(format_followup_operator(case))
    assert "Document verification coverage" not in text


def test_followup_connect_timeout_omits_n_second_placeholder():
    """NEDCOM connect timeouts lack read timeout=Ns — never emit 'N-second'."""
    from diagnostic_mas.operator_report import format_followup_operator

    case = CaseFile(
        budget=Budget(max_deep_checks=0, max_handoffs=0),
        device_names=["scm-data-sw"],
    )
    open_issue(
        case,
        code="device_live_unreachable",
        message=(
            "scm-data-sw: automated NSO live-MCP collection timed out on "
            "check_isis_adjacencies [check_isis_adjacencies: RESTCONF 500: "
            "Failed to connect to device scm-data-sw: connection refused: "
            "NEDCOM CONNECT: Connect timed out in new state]. "
            "NSO still has inventory/config; further live MCP skipped."
        ),
        evidence_ids=[],
        layer="devices",
        devices=["scm-data-sw"],
    )
    text = "\n".join(format_followup_operator(case))
    assert "N-second" not in text
    assert "connect/timeout failure" in text
    assert "scm-data-sw" in text
    assert "IS-IS collection" in text or "live MCP collection" in text


def test_followup_prioritizes_reachability_then_services_over_mapping():
    from diagnostic_mas.operator_report import format_followup_operator

    case = CaseFile(
        budget=Budget(max_deep_checks=0, max_handoffs=0),
        device_names=["scm-data-sw", "star-data-sw", "wash-data-sw"],
    )
    open_issue(
        case,
        code="unknown_neighbor_address",
        message="wash-data-sw 10.1.1.1: could not map",
        evidence_ids=[],
        layer="routing",
        devices=["wash-data-sw"],
    )
    open_issue(
        case,
        code="device_live_unreachable",
        message=(
            "scm-data-sw: live query timed out this run "
            "(exec_show (isis adjacency): HTTPSConnectionPool Read timed out. "
            "(read timeout=10)). NSO still has inventory/config."
        ),
        evidence_ids=[],
        layer="devices",
        devices=["scm-data-sw"],
    )
    open_issue(
        case,
        code="device_live_unreachable",
        message=(
            "exec_show (isis adjacency): HTTPSConnectionPool Read timed out. "
            "(read timeout=10)"
        ),
        evidence_ids=[],
        layer="devices",
        devices=["star-data-sw"],
    )
    open_issue(
        case,
        code="service_unknown",
        message="l3rt svc-1: unknown (sync timeout)",
        evidence_ids=[],
        layer="services",
        edge_id="svc-1",
        devices=["star-data-sw"],
    )
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {
                "extra": {
                    "services": {
                        "l3rt/svc-1": {
                            "name": "svc-1",
                            "service_type": "l3rt",
                            "status": "unknown",
                            "system_status": "unknown",
                            "device_sync": {
                                "star-data-sw": "error: Read timed out.",
                            },
                        }
                    }
                }
            },
        },
    )
    case.service_coverage = {"svc-1": "collection_concluded"}
    text = "\n".join(format_followup_operator(case))
    assert "Investigate the repeated 10-second NSO API timeout during SCM" in text
    assert "IS-IS collection" in text
    assert "compare the same operation through MCP and the NSO CLI" in text
    assert "Investigate the repeated 10-second NSO API timeout during STAR" in text
    assert "clear collection gaps for up to 1 unknown service" in text
    assert "Restore live reachability" not in text
    assert "Retry endpoint sync/MCP for" not in text
    assert "After successful checks, reassess" not in text
    assert "Retry service sync checks" not in text
    assert text.index("STAR") < text.index("SCM")
    assert text.index("SCM") < text.index("neighbor-mapping") or text.index(
        "SCM"
    ) < text.index("Classify")


def test_followup_skips_drop_counter_only_device_reviews():
    from diagnostic_mas.operator_report import format_followup_operator

    case = CaseFile(
        budget=Budget(max_deep_checks=0, max_handoffs=0),
        device_names=["atla-data-sw"],
    )
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "device_health",
            "layer": "devices",
            "payload": {
                "extra": {
                    "topology": {
                        "operational": {
                            "layers": {
                                "physical": {
                                    "edges": [
                                        {
                                            "id": "e1",
                                            "local": {
                                                "device": "atla-data-sw",
                                                "iface": "Hu0/0/0/0",
                                            },
                                            "remote": {
                                                "device": "wash-data-sw",
                                                "iface": "Hu0/0/0/1",
                                            },
                                            "state": {"status": "up"},
                                        }
                                    ]
                                },
                                "underlay": {"edges": []},
                                "routing": {"edges": []},
                            }
                        },
                        "static": {"layers": {}},
                    },
                    "hardware_health": {
                        "atla-data-sw": {
                            "temperature": [{"ok": True}],
                            "fans": [{"ok": True}],
                            "power": [{"ok": True}],
                            "control_plane": [{"dropped": 999_999}],
                        }
                    },
                    "fleet_sync": {
                        "status": "success",
                        "data": {
                            "devices": [
                                {"device": "atla-data-sw", "result": "in-sync"},
                            ]
                        },
                    },
                }
            },
        },
    )
    text = "\n".join(format_followup_operator(case))
    assert "Classify inventory/mapping or counter observations" not in text


def test_report_run_details_includes_drills():
    case = CaseFile(
        budget=Budget(
            max_deep_checks=5,
            max_handoffs=5,
            max_drill_issues=2,
            max_tools_per_drill=30,
            max_dataplane_tools=40,
        )
    )
    case.budget.drills_used = 1
    case.budget.drill_issues_used = 1
    case.budget.dataplane_tools_used = 63
    text = render_report(case)
    assert "## Run details" in text
    assert "Drill investigations:" in text
    assert "1 / 2" in text
    assert "(tools used 1)" in text
    assert "**Dataplane dig tools:** 63 used · 40 allowed per dig" in text
    assert "**Drill investigations:** 1 / 2 (tools used 1)" in text
    assert "Dataplane tool budget:" not in text
    assert "## Budget" not in text
    assert "Evidence records:" in text


def test_incomplete_timeout_next_action_not_tool_budget():
    from diagnostic_mas.operator_report import (
        _incomplete_dig_followup,
        _incomplete_dig_next_action,
        format_followup_operator,
        format_services_operator,
    )

    dx = {
        "name": "l2-STS-timeout",
        "dataplane_status": "unknown",
        "complete": False,
        "cause": (
            "Dataplane verification incomplete because the LLM request timed out. "
            "Service forwarding status remains unknown."
        ),
        "observed": "LLM did not conclude (chat request timed out)",
    }
    next_action = _incomplete_dig_next_action(dx)
    assert "timeout" in next_action.lower()
    assert "do not raise --max-dataplane-tools" in next_action
    assert "higher dataplane tool budget" not in next_action
    follow = _incomplete_dig_followup("l2-STS-timeout", dx)
    assert "timeout" in follow.lower()
    assert "tool-budget" in follow.lower() or "not a tool-budget" in follow.lower()

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {
                "extra": {
                    "services": {
                        "l2sts/l2-STS-timeout": {
                            "name": "l2-STS-timeout",
                            "service_type": "l2sts",
                            "system_status": "up",
                            "dataplane_status": "unknown",
                            "status": "unknown",
                        }
                    }
                }
            },
        },
    )
    add_diagnosis(
        case,
        kind="dataplane",
        source="fallback",
        status="unknown",
        observed=dx["observed"],
        cause=dx["cause"],
        subject={"name": "l2-STS-timeout", "service_type": "l2sts"},
        extra={"complete": False},
    )
    svc = "\n".join(format_services_operator(case, services_detail=True))
    assert "Address the dataplane LLM timeout first" in svc
    assert "higher dataplane tool budget" not in svc
    fu = "\n".join(format_followup_operator(case))
    assert "after fixing the Fabric chat timeout" in fu
    assert "PE-side verification can finish" in fu
    assert "Raise investigation budget" not in fu


def test_incomplete_gap_result_query_vs_customer_traffic():
    from diagnostic_mas.operator_report import (
        _incomplete_dig_gap_kind,
        _incomplete_dig_next_action,
        _incomplete_result_line,
        format_services_operator,
    )

    query_dx = {
        "dataplane_status": "unknown",
        "complete": False,
        "cause": (
            "Cause unresolved: XR l2fib show client failed with data-corruption "
            "tracebacks; forwarding read is not trustworthy."
        ),
        "observed": "l2fib_show_client data inconsistency on both PEs",
    }
    assert _incomplete_dig_gap_kind(query_dx) == "query_failure"
    assert "blocked by query/object failure" in _incomplete_result_line(query_dx)
    assert "BGP summary" in _incomplete_dig_next_action(query_dx)

    traffic_dx = {
        "dataplane_status": "unknown",
        "complete": False,
        "cause": (
            "No fault demonstrated; both BD MAC tables are empty with no type-2 "
            "install; flood-list membership not checked; customer traffic "
            "delivery unverified."
        ),
        "observed": "empty BD MAC tables both PEs; IMET only",
    }
    assert _incomplete_dig_gap_kind(traffic_dx) == "needs_traffic"
    assert "requires customer-generated traffic" in _incomplete_result_line(
        traffic_dx
    )
    assert "PE-loopback" in _incomplete_dig_next_action(traffic_dx)

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {
                "extra": {
                    "services": {
                        "l2sts/svc-query": {
                            "name": "svc-query",
                            "service_type": "l2sts",
                            "system_status": "up",
                            "dataplane_status": "unknown",
                            "status": "unknown",
                        },
                        "l2sts/svc-traffic": {
                            "name": "svc-traffic",
                            "service_type": "l2sts",
                            "system_status": "up",
                            "dataplane_status": "unknown",
                            "status": "unknown",
                        },
                    }
                }
            },
        },
    )
    add_diagnosis(
        case,
        kind="dataplane",
        source="agent",
        status="unknown",
        observed=query_dx["observed"],
        cause=query_dx["cause"],
        subject={"name": "svc-query", "service_type": "l2sts"},
        extra={"complete": False},
    )
    add_diagnosis(
        case,
        kind="dataplane",
        source="agent",
        status="unknown",
        observed=traffic_dx["observed"],
        cause=traffic_dx["cause"],
        subject={"name": "svc-traffic", "service_type": "l2sts"},
        extra={"complete": False},
    )
    text = "\n".join(format_services_operator(case, services_detail=True))
    assert "blocked by query/object failure" in text
    assert "requires customer-generated traffic" in text
    assert "Confirmed fault" not in text


def test_report_keeps_hypotheses_in_run_details():
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    case.hypotheses.append(
        {
            "id": "hy_1",
            "text": "fiber cut between A and B",
            "issue_ids": [],
        }
    )
    text = render_report(case)
    assert "Open hypotheses" in text
    assert "fiber cut" in text
    assert text.index("## Run details") < text.index("fiber cut")


def test_report_hides_hypotheses_when_drill_concluded():
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    iid_l2 = open_issue(
        case,
        code="service_degraded",
        message="l2ptp bad",
        evidence_ids=[],
        edge_id="l2-bad",
    )
    iid_addr = open_issue(
        case,
        code="unknown_neighbor_address",
        message="10.148.0.1",
        evidence_ids=[],
        edge_id=None,
    )
    case.hypotheses.extend(
        [
            {
                "id": "hy_1",
                "text": "DN side AC down due to interface failure",
                "issue_ids": [iid_l2],
            },
            {
                "id": "hy_2",
                "text": "Neighbor IP 10.148.0.1 mapping failure",
                "issue_ids": [iid_addr],
            },
        ]
    )
    add_evidence(
        case,
        {
            "kind": "drill_finding",
            "role": "drill",
            "payload": {
                "observed": "XC DN AC UP remote None",
                "cause": "missing remote binding",
                "issue_id": iid_l2,
                "issue_edge_id": "l2-bad",
            },
        },
    )
    text = render_report(case)
    assert "interface failure" not in text
    assert "10.148.0.1 mapping failure" in text


def test_report_dedupes_identical_evidence():
    case = CaseFile(budget=Budget(max_deep_checks=5, max_handoffs=5))
    payload = {
        "operational_summary": {"up": 1, "down": 0},
        "static_summary": {"n": 1},
    }
    add_evidence(
        case,
        {"kind": "spine", "role": "isis", "layer": "underlay", "payload": payload},
    )
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "isis",
            "layer": "underlay",
            "payload": dict(payload),
        },
    )
    assert len(dedupe_evidence(case.evidence)) == 1
    text = render_report(case)
    assert "Evidence records:" in text
    assert "spine role=" not in text


def test_services_table_helper_still_works():
    counts = {
        "l2ptp": {"up": 2, "down": 0, "degraded": 1, "unknown": 0},
        "l3rt": {"up": 1, "down": 1, "degraded": 0, "unknown": 0},
    }
    table = "\n".join(format_services_table(counts))
    assert "Sync In" in table
    assert "Total" in table
    assert "| up |" not in table
    assert "l2ptp" in table and "l3rt" in table
    # l2ptp: 2+0+1+0 = 3
    assert re.search(r"\|\s*l2ptp\s*\|\s*3\s*\|", table)


def test_dataplane_dig_line_counts():
    from diagnostic_mas.report import format_dataplane_dig_line

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    case.service_coverage = {
        "svc-pass": "investigated",
        "svc-incomplete": "unresolved",
        "svc-skip-a": "basic_passed",
        "svc-skip-b": "category_peer_skipped",
        "svc-skip-c": "budget_skipped",
        "svc-skip-d": "basic_passed",
    }
    add_diagnosis(
        case,
        kind="dataplane",
        source="llm",
        status="up",
        observed="ok",
        cause="none",
        subject={"name": "svc-pass", "service_type": "l2ptp"},
        extra={"complete": True},
    )
    add_diagnosis(
        case,
        kind="dataplane",
        source="llm",
        status="unknown",
        observed="timeout",
        cause="unresolved",
        subject={"name": "svc-incomplete", "service_type": "l3rt"},
        extra={"complete": False},
    )
    line = format_dataplane_dig_line(case)
    assert line == (
        "Configuration sync and dataplane are independent; sync is not "
        "dataplane verification. Only 2 services received additional "
        "dataplane investigation: 1 passed PE-side readiness checks; "
        "1 inconclusive/incomplete. Customer traffic delivery was not tested."
    )


def test_dataplane_dig_line_all_passed():
    from diagnostic_mas.report import format_dataplane_dig_line

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    for name in ("a", "b", "c"):
        add_diagnosis(
            case,
            kind="dataplane",
            source="llm",
            status="up",
            observed="ok",
            cause="none",
            subject={"name": name, "service_type": "l2ptp"},
            extra={"complete": True},
        )
    line = format_dataplane_dig_line(case)
    assert line == (
        "Configuration sync and dataplane are independent; sync is not "
        "dataplane verification. Only 3 services received additional "
        "dataplane investigation; all passed PE-side readiness checks. "
        "Customer traffic delivery was not tested."
    )

def test_operator_services_from_diagnosis():
    case = CaseFile(
        budget=Budget(max_deep_checks=0, max_handoffs=0),
        device_names=["lbnl-data-sw", "renc-data-sw"],
    )
    services = {
        "l2ptp/fabric-l2ptp-t1": {
            "service_type": "l2ptp",
            "name": "fabric-l2ptp-t1",
            "status": "up",
            "system_status": "up",
            "dataplane_status": "up",
            "devices": ["lbnl-data-sw", "renc-data-sw"],
            "live_l2": {
                "endpoints": [
                    {
                        "device": "lbnl-data-sw",
                        "ac": "Hu0/0/0/9.1001",
                        "st": "UP",
                        "xconnect": "evpn_vpws_9002",
                    },
                    {
                        "device": "renc-data-sw",
                        "ac": "Hu0/0/0/0.1002",
                        "st": "UP",
                        "xconnect": "evpn_vpws_9002",
                    },
                ]
            },
        }
    }
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {
                "operational_summary": {
                    "l2ptp": {"up": 1, "down": 0, "degraded": 0, "unknown": 0}
                },
                "extra": {"services": services},
            },
        },
    )
    add_diagnosis(
        case,
        kind="dataplane",
        source="llm",
        status="up",
        observed="Both ACs UP, xconnect UP",
        cause="No dataplane fault found",
        subject={"name": "fabric-l2ptp-t1", "service_type": "l2ptp"},
        extra={"complete": True},
    )
    text = render_report(case, services_detail=True, duration_seconds=260)
    assert text.startswith("# NSO Diagnostic Report\n")
    assert "**Duration:** 4 min 20 sec" in text
    assert "## Devices" in text
    assert "## Services" in text
    # Per-type summary table restored at top of Services
    assert "| Service type | Total | OpUp | Down | Degraded | Unknown |" in text
    assert "Sync out/unknown has final status Unknown" in text
    assert "| l2ptp" in text
    assert text.index("## Services") < text.index("| l2ptp")
    assert text.index("| l2ptp") < text.index(
        "received additional dataplane investigation"
    )
    assert (
        "Configuration sync and dataplane are independent; sync is not "
        "dataplane verification. Only 1 service received additional "
        "dataplane investigation; all passed PE-side readiness checks. "
        "Customer traffic delivery was not tested."
    ) in text
    assert text.index("received additional dataplane investigation") < text.index(
        "### L2PTP · fabric-l2ptp-t1"
    )
    assert text.index("| l2ptp") < text.index("### L2PTP · fabric-l2ptp-t1")
    assert "### L2PTP · fabric-l2ptp-t1" in text
    assert "**Result:** Passed PE-side readiness checks." in text
    assert "**Uncertainty:** Customer traffic delivery was not tested." in text
    assert (
        "Obtain a known customer endpoint and perform a scoped reachability"
    ) in text
    assert "(human must approve any config change)" not in text
    assert "*Service ID: `fabric-l2ptp-t1`*" in text
    assert "## Recommended follow-up" in text
    assert "## Device Health" not in text
    assert "## Service details" not in text
    assert text.index("## Devices") < text.index("## Services")
    assert text.index("## Services") < text.index("## Recommended follow-up")
    assert text.index("## Recommended follow-up") < text.index("## Run details")
    assert "## Changes since previous run" in text
    assert text.index("## Changes since previous run") < text.index("## Devices")


def test_report_title_includes_llm_model_when_provided():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    plain = render_report(case)
    assert plain.startswith("# NSO Diagnostic Report\n")
    assert "(with " not in plain.split("\n", 1)[0]

    with_model = render_report(case, llm_model="google/claude-opus-5")
    assert with_model.startswith(
        "# NSO Diagnostic Report (with google/claude-opus-5)\n"
    )

    blank = render_report(case, llm_model="  ")
    assert blank.startswith("# NSO Diagnostic Report\n")


def test_ac_not_found_uncertainty_and_up_next_omit_approve():
    """ac_not_found: contradiction wording; up Next has no config-approve footer."""
    from diagnostic_mas.operator_report import format_followup_operator

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    services = {
        "l2sts/p4-kans": {
            "name": "p4-kans",
            "service_type": "l2sts",
            "system_status": "up",
            "dataplane_status": "up",
            "status": "up",
            "live_l2": {
                "endpoints": [
                    {
                        "device": "utah-data-sw",
                        "ac": "Hu0/0/0/24/1.0",
                        "error": "ac_not_found",
                    },
                    {"device": "kans-data-sw", "ac": "Hu0/0/0/2.0", "st": "UP"},
                ]
            },
        }
    }
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {
                "operational_summary": {
                    "l2sts": {"up": 1, "down": 0, "degraded": 0, "unknown": 0}
                },
                "extra": {"services": services},
            },
        },
    )
    add_diagnosis(
        case,
        kind="dataplane",
        source="llm",
        status="up",
        observed="Both ACs up in BD",
        cause="PE-side ready",
        subject={"name": "p4-kans", "service_type": "l2sts"},
        extra={"complete": True},
        fix_suggestion=(
            "Obtain a known customer endpoint and perform a scoped "
            "reachability test."
        ),
    )
    text = render_report(case, services_detail=True)
    assert "**Evidence:**" in text
    assert (
        "Initial probe returned `ac_not_found`; contradicted by "
        "service-specific checks."
    ) in text
    assert "**Uncertainty:**" in text
    assert "- Customer traffic delivery was not tested." in text
    assert "**Collector detail:**" in text
    assert "Collector notes: ac_not_found." in text
    # Essential evidence beside the finding; collector/device detail later.
    assert text.index("**Evidence:**") < text.index("**Collector detail:**")
    assert text.index("**Uncertainty:**") < text.index("**Collector detail:**")
    assert "Investigate the L2 lookup path" not in text
    assert "probe-naming" not in text.lower()
    assert "(human must approve any config change)" not in text
    follow = "\n".join(format_followup_operator(case))
    # Complete dig with residual ac_not_found is Services uncertainty, not a
    # "finish verification" follow-up (avoids 9 incomplete vs 10 follow-up).
    assert "initial `ac_not_found` contradicted by service-specific checks" not in follow
    assert "Finish verification" not in follow
    assert "Investigate the collector discrepancy" not in follow


def test_followup_incomplete_count_excludes_up_dig_with_ac_not_found():
    """nso24: follow-up N must match incomplete digs, not +ac_not_found ups."""
    from diagnostic_mas.operator_report import format_followup_operator

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    services = {}
    for i in range(9):
        name = f"inc-{i}"
        services[f"l2sts/{name}"] = {
            "name": name,
            "service_type": "l2sts",
            "status": "up",
            "system_status": "up",
            "dataplane_status": "unknown",
        }
    services["l2sts/up-ac"] = {
        "name": "up-ac",
        "service_type": "l2sts",
        "status": "up",
        "system_status": "up",
        "dataplane_status": "up",
        "live_l2": {
            "endpoints": [
                {"device": "a-sw", "ac": "Hu0/0/0/1.0", "error": "ac_not_found"},
                {"device": "b-sw", "ac": "Hu0/0/0/2.0", "st": "UP"},
            ]
        },
    }
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {"extra": {"services": services}},
        },
    )
    for i in range(9):
        add_diagnosis(
            case,
            kind="dataplane",
            source="llm",
            status="unknown",
            subject={"name": f"inc-{i}", "service_type": "l2sts"},
            cause="gap",
            observed="gap",
            extra={"complete": False},
        )
    add_diagnosis(
        case,
        kind="dataplane",
        source="llm",
        status="up",
        subject={"name": "up-ac", "service_type": "l2sts"},
        cause="ready",
        observed="ACs up",
        extra={"complete": True},
    )
    follow = "\n".join(format_followup_operator(case))
    assert "Finish verification for 9" in follow
    assert "Finish verification for 10" not in follow


def test_l2sts_evidence_shows_identity_and_directions_before_collector():
    """L2STS dig: AC/BD/EVI + per-direction route exchange beside finding."""
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    services = {
        "l2sts/p4-kans": {
            "name": "p4-kans",
            "service_type": "l2sts",
            "system_status": "up",
            "dataplane_status": "up",
            "status": "up",
            "live_l2": {
                "endpoints": [
                    {
                        "device": "utah-data-sw",
                        "ac": "Hu0/0/0/24/1.0",
                        "error": "ac_not_found",
                    },
                    {"device": "kans-data-sw", "ac": "Hu0/0/0/2.0", "st": "UP"},
                ]
            },
        }
    }
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {
                "operational_summary": {
                    "l2sts": {"up": 1, "down": 0, "degraded": 0, "unknown": 0}
                },
                "extra": {"services": services},
            },
        },
    )
    add_diagnosis(
        case,
        kind="dataplane",
        source="llm",
        status="up",
        observed=(
            "- Identity: utah Hu0/0/0/24/1.0 + kans Hu0/0/0/2.0; "
            "BD bg-P4_KANS_NET:bd-P4_KANS_NET; EVI 9037\n"
            "- Route exchange utah→kans: utah local MACs installed as EVPN "
            "in kans BD forwarding\n"
            "- Route exchange kans→utah: peer EVPN routes accepted into utah "
            "BD (not AC counters alone)\n"
            "- Unverified: customer traffic delivery"
        ),
        cause="PE-side ready; traffic delivery unverified",
        subject={"name": "p4-kans", "service_type": "l2sts"},
        extra={"complete": True},
    )
    text = render_report(case, services_detail=True)
    assert "**Evidence:**" in text
    assert "Identity:" in text
    assert "EVI 9037" in text
    assert "Route exchange utah→kans:" in text
    assert "Route exchange kans→utah:" in text
    assert "Unverified: customer traffic delivery" in text
    assert "**Uncertainty:**" in text
    assert "Customer traffic delivery was not tested" in text
    assert text.index("**Evidence:**") < text.index("**Collector detail:**")
    assert text.index("Route exchange utah→kans:") < text.index(
        "**Collector detail:**"
    )
    assert "**Observed:**" not in text


def test_down_dataplane_next_keeps_config_approve():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {
                "extra": {
                    "services": {
                        "l2ptp/bad": {
                            "name": "bad",
                            "service_type": "l2ptp",
                            "system_status": "up",
                            "dataplane_status": "down",
                            "status": "down",
                        }
                    }
                }
            },
        },
    )
    add_diagnosis(
        case,
        kind="dataplane",
        source="llm",
        status="down",
        observed="XC DN",
        cause="Remote segment down",
        subject={"name": "bad", "service_type": "l2ptp"},
        extra={"complete": True},
        fix_suggestion="Verify EVPN remote binding on peer PE.",
    )
    text = render_report(case, services_detail=True)
    assert "(human must approve any config change)" in text


def test_report_summary_before_body():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    text = render_report(
        case,
        summary="Two services passed additional operational checks.",
    )
    assert "## Summary" in text
    assert "Two services passed" in text
    assert "## Changes since previous run" in text
    assert text.index("## Summary") < text.index("## Changes since previous run")
    assert text.index("## Changes since previous run") < text.index("## Devices")


def test_report_never_carries_the_detailed_device_appendix():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    text = render_report(case)
    assert "Detailed Device Analysis" not in text
    assert "--full" not in text


def test_result_and_followup_group_unknowns_by_endpoint():
    """Headline keeps endpoint combinations; follow-up is one action per device."""
    from typing import Any

    from diagnostic_mas.operator_report import format_followup_operator

    services: dict[str, Any] = {}
    for i in range(5):
        services[f"l3rt/gpn-{i}"] = {
            "name": f"gpn-{i}",
            "service_type": "l3rt",
            "status": "unknown",
            "system_status": "unknown",
            "device_sync": {"gpn-data-sw": "error: sync check failed"},
        }
    for i in range(3):
        services[f"l3rt/star-{i}"] = {
            "name": f"star-{i}",
            "service_type": "l3rt",
            "status": "unknown",
            "system_status": "unknown",
            "device_sync": {
                "star-data-sw": "error: Read timed out. (read timeout=10)"
            },
        }
    for i in range(2):
        services[f"l3rt/eduky-{i}"] = {
            "name": f"eduky-{i}",
            "service_type": "l3rt",
            "status": "unknown",
            "system_status": "unknown",
            "device_sync": {"eduky-data-sw": "error: boom"},
        }
    services["l3rt/both"] = {
        "name": "both",
        "service_type": "l3rt",
        "status": "unknown",
        "system_status": "unknown",
        "device_sync": {
            "star-data-sw": "error: Read timed out.",
            "toky-data-sw": "error: boom",
        },
    }
    services["l3rt/ok"] = {
        "name": "ok",
        "service_type": "l3rt",
        "status": "up",
        "system_status": "up",
        "device_sync": {"atla-data-sw": "in-sync"},
    }

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_evidence(
        case,
        {
            "kind": "spine",
            "role": "service",
            "layer": "services",
            "payload": {"extra": {"services": services}},
        },
    )
    # Quarantine-only device (timeout) with no unknown-service attribution.
    open_issue(
        case,
        code="device_live_unreachable",
        message="Read timed out. (read timeout=10)",
        evidence_ids=[],
        devices=["scm-data-sw"],
        severity="warning",
    )
    # No service_coverage — mirrors spine-only / skip-llm runs.
    result = format_result_line(case)
    # The headline shares the table's final counts. The legacy sync-only
    # "ok" record has no operational proof, so it is also Unknown.
    assert "Final service status: 0 OpUp, 0 Down, 0 Degraded, 12 Unknown." in result

    follow = "\n".join(format_followup_operator(case))
    # One action per device; combination rows stay out of follow-up.
    assert (
        "Restore live collection evidence for `gpn-data-sw` to clear up "
        "to 5 unknown services"
    ) in follow
    assert (
        "Restore live collection evidence for `star-data-sw` to clear up "
        "to 4 unknown services"
    ) in follow
    assert (
        "Restore live collection evidence for `eduky-data-sw` to clear up "
        "to 2 unknown services"
    ) in follow
    assert (
        "Restore live collection evidence for `toky-data-sw` to clear up "
        "to 1 unknown service"
    ) in follow
    assert "star-data-sw` + `toky-data-sw`" not in follow
    assert (
        "Investigate the repeated 10-second NSO API timeout during SCM"
    ) in follow
    assert "compare the same operation through MCP and the NSO CLI" in follow
    assert "After successful checks, reassess" not in follow
    assert "Per-device affected-service counts overlap" not in follow
    assert "Retry service sync checks" not in follow
    # Largest involvement first; quarantine-only after attributed devices.
    assert follow.index("gpn-data-sw") < follow.index("eduky-data-sw")
    assert follow.index("toky-data-sw") < follow.index("SCM")
    assert "not device down" in follow

    compact = render_report(case)
    assert "| Service type | Total | OpUp | Down | Degraded | Unknown |" in compact
    assert "### Incomplete collection checks" in compact
    assert "`gpn-data-sw` · sync check error — 5 services" in compact
    assert "`star-data-sw` · sync check timed out — 3 services" in compact
    assert "`star-data-sw` + `toky-data-sw`" in compact
    assert "### L3RT · gpn-0" not in compact
    assert "### L3RT · ok" not in compact
    assert "--services-detail" not in compact
    assert compact.index("### Incomplete collection checks") < compact.index(
        "## Recommended follow-up"
    )

    detailed = render_report(case, services_detail=True)
    assert "### L3RT · gpn-0" in detailed
    assert "### L3RT · ok" in detailed
    assert "### Incomplete collection checks" in detailed


def test_format_duration_and_result():
    assert format_duration(260) == "4 min 20 sec"
    assert format_duration(45) == "45 sec"
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    add_diagnosis(
        case,
        kind="dataplane",
        source="llm",
        status="up",
        observed="ok",
        cause="ok",
        subject={"name": "svc-a", "service_type": "l2ptp"},
        extra={"complete": True},
    )
    assert "No fault identified" in format_result_line(case)


def test_dry_run_note_in_run_details():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    text = render_report(case, dry_run=True)
    assert "Dry run" in text


def test_diagnostic_state_dir_under_diagnostic_mas():
    class S:
        state_dir = "/tmp/nso-state"

    path = diagnostic_mas_state_dir(S())  # type: ignore[arg-type]
    assert path.as_posix().endswith("diagnostic_mas")


def test_service_faults_precede_collection_and_are_not_collapsed_into_gaps():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    open_issue(case, code="device_live_unreachable", message="connect timeout",
               evidence_ids=[], devices=["scm-data-sw"], layer="underlay")
    for i in range(5):
        add_diagnosis(case, kind="dataplane", source="llm", status="unknown",
                      observed="gap", cause="missing check", subject={"name": f"gap-{i}"},
                      extra={"complete": False})
    add_diagnosis(case, kind="dataplane", source="llm", status="down",
                  observed="XC down", cause="transport unavailable",
                  subject={"name": "faulty-circuit"})
    case.last_known_service_faults = [{
        "service": "previous-fault", "status": "degraded",
        "last_observed_run_id": "r1", "rechecked": False,
        "verification": "not rechecked; last-known fault unresolved",
    }]
    lines = format_followup_operator(case)
    assert "faulty-circuit" in lines[0]
    assert "previous-fault" in lines[1]
    assert any("scm-data-sw" in line for line in lines[2:])
    assert "5" in next(line for line in lines if "incomplete" in line)
    assert sum("faulty-circuit" in line for line in lines) == 1
    text = render_report(case, include_changes_section=False)
    assert "last known degraded" in text
    assert "run `r1`" in text


def test_collection_fault_precedes_device_collection():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    open_issue(case, code="device_live_unreachable", message="connect timeout",
               evidence_ids=[], devices=["scm-data-sw"], layer="underlay")
    open_issue(case, code="service_down", message="AC down",
               evidence_ids=[], edge_id="collection-fault", layer="services")
    assert "collection-fault" in format_followup_operator(case)[0]


def test_summary_heading_is_owned_by_renderer():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    body = "Finding retained.\n\n### Next steps\nCheck the evidence."
    for prefix in ("", "## Summary\n\n", "# summary ###\n## Summary\n"):
        text = render_report(case, summary=prefix + body)
        assert text.count("## Summary\n") == 1
        assert body in text


def test_service_layers_keep_sync_independent_and_require_dig_evidence():
    from diagnostic_mas.report import service_layer_counts
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    services = {}
    for i in range(7):
        services[f'l2ptp/s{i}'] = dict(service_type='l2ptp', name=f's{i}',
            devices=['a', 'b'], device_sync={'a': 'in-sync', 'b': 'in-sync'},
            status='down' if i < 3 else 'up', dataplane_status='up')
        case.diagnoses.append(dict(kind='dataplane', subject={'name': f's{i}'},
                                   status='down' if i < 3 else 'up', complete=True))
    services['l2sts/unexamined'] = dict(service_type='l2sts', name='unexamined',
        devices=['a', 'b'], device_sync={'a': 'in-sync'}, dataplane_status='up')
    services['l2sts/incomplete'] = dict(service_type='l2sts', name='incomplete',
        devices=['a', 'b'], device_sync={'a': 'out-of-sync', 'b': 'unknown'})
    case.diagnoses.append(dict(kind='dataplane', subject={'name': 'incomplete'},
                               status='up', complete=False))
    case.evidence.append(dict(kind='spine', role='service', payload={'extra': {'services': services}}))
    counts = service_layer_counts(case)
    assert counts['l2ptp']['sync_in'] == 7
    assert counts['l2ptp']['dp_up'] == 4
    assert counts['l2ptp']['dp_down'] == 3
    assert counts['l2sts']['sync_out'] == 1
    assert counts['l2sts']['sync_unknown'] == 1
    assert counts['l2sts']['dp_unknown'] == 1
    assert counts['l2sts']['dp_not_checked'] == 1
    for b in counts.values():
        assert sum(b[k] for k in ('sync_in', 'sync_out', 'sync_unknown')) == b['total']
        assert sum(b[k] for k in ('dp_up', 'dp_down', 'dp_degraded', 'dp_unknown', 'dp_not_checked')) == b['total']


def test_scrub_internal_ids_keeps_sub_bullet_indentation():
    raw = (
        "- **l2bridge a** — dataplane=down\n"
        "  - Cause: no receive  light\n"
        "  - Next: check the fibre"
    )
    assert scrub_internal_ids(raw) == (
        "- **l2bridge a** — dataplane=down\n"
        "  - Cause: no receive light\n"
        "  - Next: check the fibre"
    )


_SUMMARY_WITH_SUB_BULLETS = (
    "Seven l2bridge services are down.\n\n"
    "- Next: review collection failures\n"
    "- **l2bridge a** — dataplane=down\n"
    "  - Cause: no receive light\n"
    "  - Next: check the fibre\n"
)


def test_summary_sub_bullet_labels_are_bold():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    text = render_report(case, summary=_SUMMARY_WITH_SUB_BULLETS)
    assert "  - **Cause:** no receive light" in text
    assert "  - **Next:** check the fibre" in text
    # Only sub-bullets are labelled; a top-level bullet is left as written.
    assert "\n- Next: review collection failures" in text


def test_summary_sub_bullet_labels_already_bold_are_unchanged():
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    summary = "- **l2bridge a** — dataplane=down\n  - **Cause:** no receive light\n"
    text = render_report(case, summary=summary)
    assert "  - **Cause:** no receive light" in text
    assert "****" not in text


def test_summary_sub_bullet_labels_are_bold_in_html_report():
    from diagnostic_mas.html_report import render_html_report

    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    doc = render_html_report(
        render_report(case, summary=_SUMMARY_WITH_SUB_BULLETS), "run"
    )
    assert "<li><strong>Cause:</strong> no receive light</li>" in doc
    assert "<li><strong>Next:</strong> check the fibre</li>" in doc
