"""Phase 1+ metrics: build gauges from a snapshot and push to Prometheus Pushgateway."""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

from nso_facts.fleet_rollups import (
    fleet_device_sync_breakdown,
    fleet_drift_and_locked,
    fleet_infra_alert_counts,
    fleet_inventory_review_count,
    fleet_routing_totals,
)
from nso_facts.hardware_health import fleet_hardware_alert_counts


class MetricsSettings(Protocol):
    prometheus_pushgateway_url: str | None
    prometheus_job: str
    prometheus_instance: str


def _intish(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _gauge(
    name: str,
    value: float,
    labels: dict[str, str] | None = None,
    *,
    pipeline: str | None = None,
) -> str:
    merged: dict[str, str] = dict(labels or {})
    if pipeline:
        merged["pipeline"] = pipeline
    if merged:
        inner = ",".join(
            f'{k}="{_escape_label(v)}"' for k, v in sorted(merged.items())
        )
        return f"{name}{{{inner}}} {value}"
    return f"{name} {value}"


def _topology_issue_counts(topology: Any) -> dict[str, int]:
    counts = {"physical": 0, "underlay": 0, "routing": 0, "services": 0}
    if not isinstance(topology, dict):
        return counts
    op = topology.get("operational") or {}
    if not isinstance(op, dict):
        return counts
    for issue in op.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        layer = issue.get("layer")
        if isinstance(layer, str) and layer in counts:
            counts[layer] += 1
    return counts


def _layer_up_down(topology: Any, layer: str) -> tuple[int, int]:
    """Prefer operational layer summary; fall back to fleet_routing_totals."""
    if isinstance(topology, dict):
        op = topology.get("operational") or {}
        if isinstance(op, dict):
            layers = op.get("layers") or {}
            if isinstance(layers, dict):
                block = layers.get(layer) or {}
                if isinstance(block, dict):
                    summary = block.get("summary")
                    if isinstance(summary, dict) and "total" in summary:
                        total = _intish(summary.get("total"))
                        up = _intish(summary.get("up"))
                        if "down" in summary:
                            down = _intish(summary.get("down"))
                        else:
                            down = max(0, total - up)
                        return up, down
    bgp_up, bgp_total, isis_up, isis_total = fleet_routing_totals(topology)
    if layer == "routing":
        return bgp_up, max(0, bgp_total - bgp_up)
    if layer == "underlay":
        return isis_up, max(0, isis_total - isis_up)
    return 0, 0


def build_phase1_metrics(
    snapshot: dict[str, Any],
    *,
    success: bool,
    duration_seconds: float,
    timestamp_seconds: float | None = None,
    pipeline: str = "agent",
) -> list[str]:
    """Return Prometheus exposition lines (gauges).

    ``pipeline`` is attached as a label on every series (``agent`` or
    ``multi-agent``) so Grafana can filter when both CLIs push.
    """
    ts = timestamp_seconds if timestamp_seconds is not None else time.time()
    topology = snapshot.get("topology")
    fleet_sync = snapshot.get("fleet_sync")
    counts = snapshot.get("counts") or {}
    system_health = snapshot.get("system_health") or {}
    hardware_health = snapshot.get("hardware_health") or {}

    total, in_sync, out_of_sync, sync_error = fleet_device_sync_breakdown(
        fleet_sync, topology
    )
    devices_total = _intish(total) if total != "—" else 0
    devices_in_sync = _intish(in_sync) if in_sync != "—" else 0
    devices_out, devices_locked = fleet_drift_and_locked(fleet_sync, _intish(out_of_sync))
    devices_err = _intish(sync_error)

    isis_up, isis_down = _layer_up_down(topology, "underlay")
    bgp_up, bgp_down = _layer_up_down(topology, "routing")
    phys_up, phys_down = _layer_up_down(topology, "physical")
    issue_counts = _topology_issue_counts(topology)
    cpu_alerts, mem_alerts = fleet_infra_alert_counts(system_health)
    temp_a, fan_a, pwr_a, cp_a = fleet_hardware_alert_counts(hardware_health)
    inventory_review = fleet_inventory_review_count(topology, fleet_sync)

    def g(name: str, value: float, labels: dict[str, str] | None = None) -> str:
        return _gauge(name, value, labels, pipeline=pipeline)

    lines: list[str] = [
        "# TYPE nso_summary_run_success gauge",
        g("nso_summary_run_success", 1.0 if success else 0.0),
        "# TYPE nso_summary_last_run_timestamp_seconds gauge",
        g("nso_summary_last_run_timestamp_seconds", float(ts)),
        "# TYPE nso_summary_run_duration_seconds gauge",
        g("nso_summary_run_duration_seconds", float(duration_seconds)),
        "# TYPE nso_fleet_devices_total gauge",
        g("nso_fleet_devices_total", float(devices_total)),
        "# TYPE nso_fleet_devices_in_sync gauge",
        g("nso_fleet_devices_in_sync", float(devices_in_sync)),
        "# TYPE nso_fleet_devices_out_of_sync gauge",
        g("nso_fleet_devices_out_of_sync", float(devices_out)),
        "# TYPE nso_fleet_devices_sync_error gauge",
        g("nso_fleet_devices_sync_error", float(devices_err)),
        "# TYPE nso_fleet_devices_locked gauge",
        g("nso_fleet_devices_locked", float(devices_locked)),
        "# TYPE nso_isis_adjacencies_up gauge",
        g("nso_isis_adjacencies_up", float(isis_up)),
        "# TYPE nso_isis_adjacencies_down gauge",
        g("nso_isis_adjacencies_down", float(isis_down)),
        "# TYPE nso_bgp_sessions_up gauge",
        g("nso_bgp_sessions_up", float(bgp_up)),
        "# TYPE nso_bgp_sessions_down gauge",
        g("nso_bgp_sessions_down", float(bgp_down)),
        "# TYPE nso_physical_links_up gauge",
        g("nso_physical_links_up", float(phys_up)),
        "# TYPE nso_physical_links_down gauge",
        g("nso_physical_links_down", float(phys_down)),
        "# TYPE nso_infra_cpu_alerts gauge",
        g("nso_infra_cpu_alerts", float(cpu_alerts)),
        "# TYPE nso_infra_memory_alerts gauge",
        g("nso_infra_memory_alerts", float(mem_alerts)),
        "# TYPE nso_hardware_temperature_alerts gauge",
        g("nso_hardware_temperature_alerts", float(temp_a)),
        "# TYPE nso_hardware_fan_alerts gauge",
        g("nso_hardware_fan_alerts", float(fan_a)),
        "# TYPE nso_hardware_power_alerts gauge",
        g("nso_hardware_power_alerts", float(pwr_a)),
        "# TYPE nso_hardware_control_plane_drop_alerts gauge",
        g("nso_hardware_control_plane_drop_alerts", float(cp_a)),
        "# TYPE nso_inventory_review_devices gauge",
        g("nso_inventory_review_devices", float(inventory_review)),
    ]

    lines += _device_infra_lines(system_health, g)

    lines += _attempt_lines(ts, success=success, nso_unreachable=False, pipeline=pipeline)

    not_covered = snapshot.get("devices_not_covered")
    if not_covered is not None:
        lines.append("# TYPE nso_fleet_devices_not_covered gauge")
        lines.append(g("nso_fleet_devices_not_covered", float(_intish(not_covered))))

    scan = snapshot.get("scan")
    if isinstance(scan, dict) and scan.get("run_id"):
        # One series per scan: lets a dashboard show and link the latest report.
        lines.append("# TYPE nso_scan_info gauge")
        lines.append(g("nso_scan_info", 1.0, {
            "run_id": str(scan["run_id"]), "report_url": str(scan.get("report_url") or "")}))

    llm = snapshot.get("llm")
    if isinstance(llm, dict) and llm.get("state") in LLM_STATES:
        lines += _llm_lines(llm, g)

    service_status = snapshot.get("service_status")
    if isinstance(service_status, dict):
        # The report's final status, one series per status; replaces the
        # older up/down pair, which lumped Degraded and Unknown into "down".
        lines.append("# TYPE nso_services gauge")
        for service_type, bucket in sorted(service_status.items()):
            for status in ("up", "down", "degraded", "unknown"):
                lines.append(g("nso_services", float(_intish(bucket.get(status))),
                               {"service_type": str(service_type), "status": status}))
        lines.append("# TYPE nso_service_faults gauge")
        for row in snapshot.get("service_faults") or []:
            lines.append(g("nso_service_faults", float(_intish(row.get("count"))), {
                "device": str(row.get("device")), "service_type": str(row.get("service_type")),
                "status": str(row.get("status"))}))
        counts = None

    if isinstance(counts, dict):
        lines.append("# TYPE nso_services_up gauge")
        lines.append("# TYPE nso_services_down gauge")
        for service_type, bucket in sorted(counts.items()):
            if not isinstance(bucket, dict):
                continue
            st = str(service_type)
            up = _intish(bucket.get("up"))
            down = (
                _intish(bucket.get("down"))
                + _intish(bucket.get("degraded"))
                + _intish(bucket.get("unknown"))
            )
            lines.append(g("nso_services_up", float(up), {"service_type": st}))
            lines.append(
                g("nso_services_down", float(down), {"service_type": st})
            )

    lines.append("# TYPE nso_topology_issues gauge")
    for layer, n in sorted(issue_counts.items()):
        lines.append(g("nso_topology_issues", float(n), {"layer": layer}))

    # Leads, not a status: a rate is the average over the gap since the previous scan.
    # Each push replaces the last, so a link that stops gaining errors drops off.
    crc_rates: list[str] = []
    state_changes: list[str] = []
    for row in snapshot.get("links_gaining_errors") or []:
        if not isinstance(row, dict):
            continue
        labels = {"device": str(row.get("device")), "port": str(row.get("port"))}
        crc = _intish((row.get("gained") or {}).get("crc"))
        minutes = _intish(row.get("minutes"))
        if crc > 0 and minutes > 0:
            crc_rates.append(g("nso_link_crc_per_minute", crc / minutes, labels))
        changes = _intish(row.get("state_changes"))
        if changes > 0:
            state_changes.append(g("nso_link_state_changes", float(changes), labels))
    if crc_rates:
        lines += ["# TYPE nso_link_crc_per_minute gauge", *crc_rates]
    if state_changes:
        lines += ["# TYPE nso_link_state_changes gauge", *state_changes]

    # The busiest port directions of the scan (the device's 5-minute rate over the port's bandwidth), a few only:
    # each push replaces the last, so a port that is no longer among them drops off.
    busy = [g("nso_link_utilization_percent", float(row["percent"]),
              {"device": str(row["device"]), "port": str(row["port"]), "direction": str(row["direction"])})
            for row in snapshot.get("link_utilization") or [] if isinstance(row, dict) and row.get("percent") is not None]
    if busy:
        lines += ["# TYPE nso_link_utilization_percent gauge", *busy]

    # The sweep of ports on links that are up: how many report an optics alarm of their own (a lead, not a status)
    # out of how many were read. Absent when the scan did not sweep, so a gap is not drawn as zero alarms.
    optics = snapshot.get("optics")
    if isinstance(optics, dict):
        lines += ["# TYPE nso_optics_alarm_ports gauge", g("nso_optics_alarm_ports", float(_intish(optics.get("alarm_ports")))),
                  "# TYPE nso_optics_ports_read gauge", g("nso_optics_ports_read", float(_intish(optics.get("ports_read"))))]

    return lines


# The value of ``nso_llm_state``: how the scan's LLM work went.
LLM_STATES = ("complete", "partial", "unavailable", "skipped", "halted")


def _llm_lines(llm: dict[str, Any], g: Any) -> list[str]:
    """The scan's LLM state, investigation counts, usage, spend and model, where each is known."""
    lines = ["# TYPE nso_llm_state gauge", g("nso_llm_state", float(LLM_STATES.index(llm["state"])))]
    for name, key in (("nso_llm_investigations_done", "done"), ("nso_llm_investigations_total", "total"),
                      ("nso_llm_requests", "requests"), ("nso_llm_input_tokens", "input_tokens"),
                      ("nso_llm_output_tokens", "output_tokens"), ("nso_llm_cost_usd", "cost"),
                      ("nso_llm_budget_left_usd", "budget_left"), ("nso_llm_budget_max_usd", "budget_max")):
        if isinstance(llm.get(key), (int, float)):
            lines += [f"# TYPE {name} gauge", g(name, float(llm[key]))]
    if llm.get("model"):
        lines += ["# TYPE nso_llm_info gauge", g("nso_llm_info", 1.0, {"model": str(llm["model"])})]
    return lines


def format_exposition(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def pushgateway_url(settings: MetricsSettings) -> str | None:
    base = (settings.prometheus_pushgateway_url or "").strip().rstrip("/")
    if not base:
        return None
    job = urllib.parse.quote(settings.prometheus_job or "nso-summary", safe="")
    instance = urllib.parse.quote(
        settings.prometheus_instance or "default", safe=""
    )
    return f"{base}/metrics/job/{job}/instance/{instance}"


# The value of ``nso_scan_last_attempt_state``: how the latest scan attempt ended.
ATTEMPT_STATES = ("ok", "failed", "nso_unreachable")


def _device_infra_lines(system_health: Any, g) -> list[str]:
    """CPU (5 minutes) and memory use per device, so a dashboard can show the busiest one.

    A device whose read failed, or that gave no reading of that kind, has no series.
    """
    cpu: list[str] = []
    memory: list[str] = []
    for device, entry in sorted((system_health if isinstance(system_health, dict) else {}).items()):
        if not isinstance(entry, dict) or entry.get("error"):
            continue
        labels = {"device": str(device)}
        five = (entry.get("cpu") or {}).get("five_min") if isinstance(entry.get("cpu"), dict) else None
        used = (entry.get("memory") or {}).get("used_pct") if isinstance(entry.get("memory"), dict) else None
        try:
            if five is not None:
                cpu.append(g("nso_device_cpu_five_min_percent", float(five), labels))
            if used is not None:
                memory.append(g("nso_device_memory_used_percent", float(used), labels))
        except (TypeError, ValueError):
            continue
    return ((["# TYPE nso_device_cpu_five_min_percent gauge", *cpu] if cpu else [])
            + (["# TYPE nso_device_memory_used_percent gauge", *memory] if memory else []))


def _attempt_lines(
    timestamp_seconds: float, *, success: bool, nso_unreachable: bool, pipeline: str
) -> list[str]:
    """How the latest scan attempt ended, whether or not it produced a report."""
    state = "nso_unreachable" if nso_unreachable else "ok" if success else "failed"
    return [
        "# TYPE nso_scan_last_attempt_timestamp_seconds gauge",
        _gauge("nso_scan_last_attempt_timestamp_seconds", float(timestamp_seconds),
               pipeline=pipeline),
        "# TYPE nso_scan_last_attempt_success gauge",
        _gauge("nso_scan_last_attempt_success", 1.0 if success else 0.0, pipeline=pipeline),
        "# TYPE nso_scan_last_attempt_nso_unreachable gauge",
        _gauge("nso_scan_last_attempt_nso_unreachable", 1.0 if nso_unreachable else 0.0,
               pipeline=pipeline),
        "# TYPE nso_scan_last_attempt_state gauge",
        _gauge("nso_scan_last_attempt_state", float(ATTEMPT_STATES.index(state)), pipeline=pipeline),
    ]


def _send(url: str, body: bytes, *, method: str) -> bool:
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except OSError:
            pass
        msg = f"warning: Prometheus Pushgateway push failed: {exc}"
        if detail:
            msg = f"{msg} — {detail}"
        print(msg, file=sys.stderr)
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"warning: Prometheus Pushgateway push failed: {exc}", file=sys.stderr)
        return False
    return True


def push_scan_attempt_failed(
    settings: MetricsSettings,
    *,
    reason: str,
    pipeline: str,
    timestamp_seconds: float | None = None,
) -> bool:
    """Tell the Pushgateway a scan ended without a report. True if pushed.

    Sent with POST, which replaces only these three metrics: the last good
    scan's values stay, so a dashboard shows them together with the failure.
    """
    url = pushgateway_url(settings)
    if not url:
        return False
    ts = timestamp_seconds if timestamp_seconds is not None else time.time()
    body = format_exposition(_attempt_lines(
        ts, success=False, nso_unreachable=reason == "nso_unreachable", pipeline=pipeline))
    return _send(url, body.encode("utf-8"), method="POST")


def push_phase1_metrics(
    snapshot: dict[str, Any],
    settings: MetricsSettings,
    *,
    success: bool,
    duration_seconds: float,
    timestamp_seconds: float | None = None,
    pipeline: str = "agent",
) -> bool:
    """Push Phase 1+ metrics. Returns True if a push was attempted and succeeded.

    Unset URL → skip (False). Transport errors → print warning, return False.
    """
    url = pushgateway_url(settings)
    if not url:
        return False

    body = format_exposition(
        build_phase1_metrics(
            snapshot,
            success=success,
            duration_seconds=duration_seconds,
            timestamp_seconds=timestamp_seconds,
            pipeline=pipeline,
        )
    ).encode("utf-8")

    return _send(url, body, method="PUT")
