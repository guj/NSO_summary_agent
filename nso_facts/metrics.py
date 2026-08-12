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


def _delta_counts(delta: Any) -> dict[str, int]:
    if not isinstance(delta, dict):
        return {
            "first_run": 0,
            "new_failures": 0,
            "recoveries": 0,
            "status_changes": 0,
            "removed": 0,
        }
    return {
        "first_run": 1 if delta.get("first_run") else 0,
        "new_failures": len(delta.get("new_failures") or []),
        "recoveries": len(delta.get("recoveries") or []),
        "status_changes": len(delta.get("status_changes") or []),
        "removed": len(delta.get("removed") or []),
    }


def build_phase1_metrics(
    snapshot: dict[str, Any],
    *,
    success: bool,
    duration_seconds: float,
    timestamp_seconds: float | None = None,
    pipeline: str = "agent",
    delta: dict[str, Any] | None = None,
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
    delta_obj = delta if delta is not None else snapshot.get("delta")

    total, in_sync, out_of_sync, sync_error = fleet_device_sync_breakdown(
        fleet_sync, topology
    )
    devices_total = _intish(total) if total != "—" else 0
    devices_in_sync = _intish(in_sync) if in_sync != "—" else 0
    devices_out = _intish(out_of_sync)
    devices_err = _intish(sync_error)

    isis_up, isis_down = _layer_up_down(topology, "underlay")
    bgp_up, bgp_down = _layer_up_down(topology, "routing")
    phys_up, phys_down = _layer_up_down(topology, "physical")
    issue_counts = _topology_issue_counts(topology)
    cpu_alerts, mem_alerts = fleet_infra_alert_counts(system_health)
    temp_a, fan_a, pwr_a, cp_a = fleet_hardware_alert_counts(hardware_health)
    inventory_review = fleet_inventory_review_count(topology, fleet_sync)
    dcounts = _delta_counts(delta_obj)

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
        "# TYPE nso_delta_first_run gauge",
        g("nso_delta_first_run", float(dcounts["first_run"])),
        "# TYPE nso_delta_new_failures gauge",
        g("nso_delta_new_failures", float(dcounts["new_failures"])),
        "# TYPE nso_delta_recoveries gauge",
        g("nso_delta_recoveries", float(dcounts["recoveries"])),
        "# TYPE nso_delta_status_changes gauge",
        g("nso_delta_status_changes", float(dcounts["status_changes"])),
        "# TYPE nso_delta_removed gauge",
        g("nso_delta_removed", float(dcounts["removed"])),
    ]

    lines.append("# TYPE nso_services_up gauge")
    lines.append("# TYPE nso_services_down gauge")
    if isinstance(counts, dict):
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


def push_phase1_metrics(
    snapshot: dict[str, Any],
    settings: MetricsSettings,
    *,
    success: bool,
    duration_seconds: float,
    timestamp_seconds: float | None = None,
    pipeline: str = "agent",
    delta: dict[str, Any] | None = None,
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
            delta=delta,
        )
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
        method="PUT",
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
