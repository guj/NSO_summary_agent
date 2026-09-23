"""CLI focus helpers: layer modes + device/service filters."""

from __future__ import annotations

from typing import Any


def resolve_spine_flags(
    *,
    isis_only: bool = False,
    bgp_only: bool = False,
    device_only: bool = False,
    service_only: bool = False,
    skip_service: bool = False,
    service_focus: bool = False,
) -> tuple[bool, bool, bool, bool]:
    """Return (run_isis, run_bgp, run_service, run_device).

    Layer modes are mutually exclusive at the CLI; this function still accepts
    combinations for tests and picks a deterministic precedence:
    isis_only > bgp_only > device_only > service_only/service_focus > default.

    ``service_focus`` is True when ``--service-type`` and/or ``--service-id``
    are set — it implies lean service-only unless a layer-only flag won.
    """
    if isis_only:
        return True, False, False, False
    if bgp_only:
        return False, True, False, False
    if device_only:
        return False, False, False, True
    if service_only or service_focus:
        return False, False, True, False
    return True, True, not skip_service, False


def endpoint_devices_from_services(services: dict[str, Any] | None) -> list[str]:
    """Unique endpoint device names from a flat services map."""
    out: list[str] = []
    seen: set[str] = set()
    for key, rec in (services or {}).items():
        if not isinstance(rec, dict):
            continue
        devices = list(rec.get("devices") or [])
        live = rec.get("live_l2") if isinstance(rec.get("live_l2"), dict) else {}
        for ep in live.get("endpoints") or []:
            if isinstance(ep, dict) and ep.get("device"):
                devices.append(ep["device"])
        for d in devices:
            name = str(d).strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def filter_device_names(
    all_names: list[str], devices_arg: str | None
) -> list[str]:
    """Filter device list by comma-separated exact names, else substring."""
    if not devices_arg or not str(devices_arg).strip():
        return list(all_names)
    wanted = [p.strip() for p in str(devices_arg).split(",") if p.strip()]
    if not wanted:
        return list(all_names)
    exact = {w.lower() for w in wanted}
    matched = [d for d in all_names if d.lower() in exact]
    if matched:
        return matched
    # Substring fallback (e.g. "renc" → renc-data-sw)
    out: list[str] = []
    seen: set[str] = set()
    for d in all_names:
        dl = d.lower()
        if any(w.lower() in dl for w in wanted) and d not in seen:
            out.append(d)
            seen.add(d)
    return out


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def service_record_matches(
    key: str,
    record: dict[str, Any],
    *,
    service_type: str | None = None,
    service_id: str | None = None,
    devices: list[str] | None = None,
) -> bool:
    if service_type:
        st = _norm(record.get("service_type") or key.split("/", 1)[0])
        want = _norm(service_type)
        if want not in st and st != want:
            return False
    if service_id:
        name = _norm(record.get("name") or record.get("id") or "")
        key_tail = _norm(key.split("/", 1)[-1] if "/" in key else key)
        want = _norm(service_id)
        if want not in name and want not in key_tail and name != want:
            return False
    if devices:
        rec_devs = {_norm(d) for d in (record.get("devices") or []) if d}
        wanted = {_norm(d) for d in devices if d}
        if wanted and not (rec_devs & wanted):
            return False
    return True


def filter_services(
    services: dict[str, Any] | None,
    *,
    service_type: str | None = None,
    service_id: str | None = None,
    devices: list[str] | None = None,
) -> dict[str, Any]:
    """Filter flat collect_service_health map (and nested instances shape)."""
    if not isinstance(services, dict):
        return {}
    if not service_type and not service_id and not devices:
        return dict(services)

    out: dict[str, Any] = {}
    for key, group in services.items():
        if not isinstance(group, dict):
            continue
        if "instances" in group:
            stype = str(key)
            if service_type and _norm(service_type) not in _norm(stype):
                continue
            kept = []
            for inst in group.get("instances") or []:
                if not isinstance(inst, dict):
                    continue
                # Synthesize a flat-shaped record for matching
                fake = {
                    "service_type": stype,
                    "name": inst.get("name") or inst.get("id"),
                    "devices": inst.get("devices") or [],
                    "status": inst.get("status"),
                }
                if service_record_matches(
                    f"{stype}/{fake.get('name')}",
                    fake,
                    service_type=None,  # already type-filtered
                    service_id=service_id,
                    devices=devices,
                ):
                    kept.append(inst)
            if kept:
                row = dict(group)
                row["instances"] = kept
                out[key] = row
            continue
        if service_record_matches(
            str(key),
            group,
            service_type=service_type,
            service_id=service_id,
            devices=devices,
        ):
            out[key] = group
    return out


def counts_from_services(services: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Per-type up/down/degraded/unknown from a flat (or nested) services map."""
    counts: dict[str, dict[str, int]] = {}

    def bump(stype: str, status: str) -> None:
        bucket = counts.setdefault(
            stype, {"up": 0, "down": 0, "degraded": 0, "unknown": 0}
        )
        st = (status or "unknown").lower()
        if st in bucket:
            bucket[st] += 1
        else:
            bucket["unknown"] += 1

    for key, group in (services or {}).items():
        if not isinstance(group, dict):
            continue
        if "instances" in group:
            stype = str(key)
            for inst in group.get("instances") or []:
                if isinstance(inst, dict):
                    bump(stype, str(inst.get("status") or "unknown"))
            continue
        stype = str(group.get("service_type") or key.split("/", 1)[0])
        bump(stype, str(group.get("status") or "unknown"))
    return counts


def filter_device_map(
    mapping: dict[str, Any] | None, devices: list[str] | None
) -> dict[str, Any]:
    if not isinstance(mapping, dict):
        return {}
    if not devices:
        return dict(mapping)
    wanted = {d.lower() for d in devices}
    return {k: v for k, v in mapping.items() if str(k).lower() in wanted}
