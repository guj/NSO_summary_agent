"""Compare two diagnostic_mas case.json artifacts (run-to-run delta)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from diagnostic_mas.case import Issue, normalize_diagnosis, normalize_issue
from diagnostic_mas.nso_authentication import authentication_failures
from nso_facts.topology.routing import unmatched_neighbor_in

# Issue statuses that still count as an unresolved problem.
_PROBLEM_STATUSES = frozenset(
    {"open", "needs_human", "budget_exhausted", "escalated"}
)

# Coverage values that mean the service was in scope / checked this run.
_CHECKED_COVERAGE = frozenset(
    {
        "basic_passed",
        "needs_investigation",
        "investigated",
        "unresolved",
        "budget_skipped",
        "llm_budget_exceeded",
        "collection_concluded",
        "category_peer_skipped",
        "fixed_check_decided",
        "port_investigated",
        "device_investigated",
    }
)

_FAULT_DP = frozenset({"down", "degraded"})
_OK_DP = frozenset({"up", "ok"})


def load_case_dict(path: Path | str) -> dict[str, Any]:
    """Load case.json from a run directory or a case.json file path."""
    p = Path(path)
    if p.is_dir():
        candidate = p / "case.json"
        if not candidate.is_file():
            raise FileNotFoundError(f"no case.json under {p}")
        p = candidate
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"case.json is not an object: {p}")
    return _shaped(data)


def _shaped(case: dict[str, Any]) -> dict[str, Any]:
    """A saved or hand-made case with its issues and diagnoses in their shapes (a copy when any changed)."""
    out = case
    for key, normalize in (("issues", normalize_issue), ("diagnoses", normalize_diagnosis)):
        rows = case.get(key)
        if isinstance(rows, list):
            shaped = [row for raw in rows if (row := normalize(raw)) is not None]
            if shaped != rows:
                out = {**out, key: shaped}
    return out


def load_previous_case(
    state_dir: Path | str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Load the last published case from ``latest.json`` under state_dir.

    Returns ``(case_dict, run_id)`` or ``(None, None)`` when unavailable.
    """
    state_dir = Path(state_dir)
    latest = state_dir / "latest.json"
    if not latest.is_file():
        return None, None
    try:
        meta = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(meta, dict):
        return None, None
    case_path = meta.get("case_path")
    run_id = meta.get("run_id")
    if not isinstance(case_path, str) or not case_path.strip():
        return None, None
    path = Path(case_path)
    if not path.is_file():
        return None, None
    try:
        return load_case_dict(path), str(run_id) if run_id else path.parent.name
    except (OSError, ValueError, json.JSONDecodeError):
        return None, None


def issue_key(issue: Issue) -> str:
    """Stable identity for matching issues across runs."""
    layer = issue["layer"] or ""
    code = issue["code"]
    edge = issue["edge_id"]
    if isinstance(edge, str) and edge.strip():
        return f"{layer}|{code}|{edge.strip()}"
    devices = issue.get("devices")
    if isinstance(devices, list) and devices:
        devs = ",".join(sorted(str(d) for d in devices))
        return f"{layer}|{code}|devices:{devs}"
    msg = issue["message"][:80]
    return f"{layer}|{code}|msg:{msg}"


def is_problem(issue: Issue) -> bool:
    return issue["status"].lower() in _PROBLEM_STATUSES


def problem_map(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for issue in case.get("issues") or []:
        if not isinstance(issue, dict) or not is_problem(issue):
            continue
        out[issue_key(issue)] = issue
    return out


def coverage_map(case: dict[str, Any]) -> dict[str, str]:
    raw = case.get("service_coverage") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _service_extras(case: dict[str, Any]):
    for ev in case.get("evidence") or []:
        if isinstance(ev, dict) and ev.get("kind") == "spine" and ev.get("role") == "service":
            yield (ev.get("payload") or {}).get("extra") or {}


def _service_records(case: dict[str, Any]):
    """(name, service type, removed while the scan ran) for each listed service."""
    for extra in _service_extras(case):
        for key, record in (extra.get("services") or {}).items():
            if not isinstance(record, dict):
                continue
            name = str(record.get("name") or str(key).split("/", 1)[-1]).strip()
            kind = str(record.get("service_type") or str(key).split("/", 1)[0]).strip()
            if name:
                yield name, kind, (record.get("presence_recheck") or {}).get("outcome") == "absent"


def _service_inventory(case: dict[str, Any]) -> dict[str, str]:
    """Service name -> service type, for services present at the end of the run."""
    return {name: kind for name, kind, removed in _service_records(case) if not removed}


def _removed_during_scan(case: dict[str, Any]) -> set[str]:
    """Services the run itself saw removed; its own report already says so."""
    return {name for name, _kind, removed in _service_records(case) if removed}


def full_service_scope(case: dict[str, Any]) -> bool:
    """True when the run recorded that it listed every service, unfiltered."""
    config = case.get("run_configuration")
    if not isinstance(config, dict) or case.get("focus_devices"):
        return False
    if any(config.get(key) != "All"
           for key in ("Device filter", "Service type filter", "Service ID filter")):
        return False
    return str(config.get("Scope flags") or "") == "Default"


def _collected_types(case: dict[str, Any]) -> set[str] | None:
    """Service types a full, unfiltered scan collected; None for a narrower scan.

    A type counts when it still has services, or when the scan recorded that
    NSO answered its listing in full, even with no instances left.
    """
    if not full_service_scope(case):
        return None
    types = set(_service_inventory(case).values())
    for extra in _service_extras(case):
        types.update(extra.get("service_types_listed") or [])
    return types


def _services_no_longer_present(older: dict[str, Any], newer: dict[str, Any]) -> dict[str, str]:
    """Services in the older inventory that a full newer scan no longer lists.

    Owners delete services routinely, so this is neither a fault nor a gap.
    Only claimed when the newer run was unfiltered and collected that
    service type; otherwise absence may just be a narrower scan.
    """
    collected = _collected_types(newer)
    if collected is None:
        return {}
    now = _service_inventory(newer)
    return {
        name: kind
        for name, kind in _service_inventory(older).items()
        if name not in now and kind in collected
    }


def _services_new_since(older: dict[str, Any], newer: dict[str, Any]) -> set[str]:
    """Services the newer scan lists that a full older scan of their type did not.

    Created since, so a fault on one is no regression. Only claimed when the
    older run was unfiltered and collected that type; otherwise it may just
    have been a narrower scan.
    """
    collected = _collected_types(older)
    if collected is None:
        return set()
    before = {name for name, _kind, _removed in _service_records(older)}
    return {name for name, kind in _service_inventory(newer).items() if name not in before and kind in collected}


# Routing protocol (spine role) -> the layer its collection errors are filed under.
_ROUTING_LAYERS = {"bgp": "routing", "isis": "underlay"}


def _routing_edges(case: dict[str, Any], role: str) -> dict[str, dict[str, Any]] | None:
    """Session id -> state for one routing protocol; None when the run did not collect it."""
    found: dict[str, dict[str, Any]] | None = None
    for ev in case.get("evidence") or []:
        if not isinstance(ev, dict) or ev.get("kind") != "spine" or ev.get("role") != role:
            continue
        found = {} if found is None else found
        for edge in (ev.get("payload") or {}).get("operational_edges") or []:
            if isinstance(edge, dict) and edge.get("id"):
                found[str(edge["id"])] = edge.get("state") or {}
    return found


def _unmatched_rows(case: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]] | None:
    """(device, neighbor) -> BGP summary row no NSO device matched; None when the run stored none."""
    found: dict[tuple[str, str], dict[str, Any]] | None = None
    for ev in case.get("evidence") or []:
        if not isinstance(ev, dict) or ev.get("kind") != "spine" or ev.get("role") != "bgp":
            continue
        rows = ((ev.get("payload") or {}).get("extra") or {}).get("unmatched_neighbors")
        if rows is None:
            continue
        found = {} if found is None else found
        for row in rows:
            if isinstance(row, dict):
                found[str(row.get("device")), str(row.get("address"))] = row
    return found


def _outside_sessions_lost(older: dict[str, Any], newer: dict[str, Any]) -> list[dict[str, Any]]:
    """Sessions to other networks' routers Established in the older run that the device now reports down.

    Only FABRIC's end is in NSO, so a row gone from the table is not evidence.
    """
    from nso_facts.topology.routing import outside_session_id

    before, now = _unmatched_rows(older), _unmatched_rows(newer)
    if not before or now is None:
        return []
    rows = []
    for key, row in sorted(before.items()):
        current = now.get(key) or {}
        if (row.get("outside") and row.get("state") == "established"
                and current.get("outside") and current.get("state") != "established"):
            rows.append({"protocol": "bgp-outside", "edge_id": outside_session_id(*key),
                         "now": str(current.get("state")), "states": "this end only",
                         "remote_as": current.get("remote_as")})
    return rows


def _devices_answering(case: dict[str, Any], layer: str) -> set[str]:
    """Devices a full, unfiltered scan queried live, less those whose query for this layer failed."""
    config = case.get("run_configuration")
    if (not isinstance(config, dict) or case.get("focus_devices")
            or config.get("Device filter") != "All"
            or str(config.get("Scope flags") or "") != "Default"):
        return set()
    failed = {
        str(issue.get("message") or "").split(":", 1)[0].strip()
        for issue in case.get("issues") or []
        if isinstance(issue, dict) and issue.get("code") == "collection_error"
        and issue.get("layer") == layer
    }
    return set(case.get("live_verified_devices") or []) - failed


def _adjacency_ends(edge_id: str) -> list[tuple[str, str]]:
    """(device, interface) at each end of an ``isis:devA:ifA:devB:ifB`` id."""
    parts = edge_id.split(":")
    return [(parts[1], parts[2]), (parts[3], parts[4])] if len(parts) == 5 and parts[0] == "isis" else []


def _session_devices(edge_id: str) -> tuple[str, str] | None:
    """The devices whose views are recorded as ``local`` and ``remote``, in the id's order."""
    parts = edge_id.split(":")
    if len(parts) != 5:
        return None
    return {"isis": (parts[1], parts[3]), "bgp": (parts[3], parts[4])}.get(parts[0])


def _seen_up_at(state: dict[str, Any]) -> str | None:
    """``local`` or ``remote``: the one end that listed a session up while the other was not read."""
    for side, other in (("local", "remote"), ("remote", "local")):
        if state.get(side) in {"up", "established"} and state.get(other) in {None, "", "unknown"}:
            return side
    return None


def _routing_sessions_lost(older: dict[str, Any], newer: dict[str, Any]) -> list[dict[str, str]]:
    """Sessions and adjacencies up in the older run that are not up in the newer one.

    A session the devices now report down is always listed. One that is
    merely absent is listed only when both of its devices answered this
    protocol's query in a full scan; otherwise absence is not evidence.
    A session up at the one end that was read is held to that end's view:
    lost when that end now reports it down, or answers without it.
    """
    known = set(older.get("device_names") or []) | set(newer.get("device_names") or [])
    rows: list[dict[str, str]] = []
    for role, layer in _ROUTING_LAYERS.items():
        before, now = _routing_edges(older, role), _routing_edges(newer, role)
        if not before or now is None:
            continue
        answering = _devices_answering(newer, layer)
        # Parallel links are also recorded under ids that pair one link's end
        # with another's, so an absent id is a loss only when neither of its
        # ends is in any adjacency now.
        still_adjacent = {
            end for current_id, current_state in now.items() if current_state.get("status") == "up"
            for end in _adjacency_ends(current_id)
        } if role == "isis" else set()
        # Once its far end is read, a one-sided adjacency is recorded under its full id.
        listed = {end for current_id in now for end in _adjacency_ends(current_id)}
        for edge_id, state in sorted(before.items()):
            side = None if state.get("status") == "up" else _seen_up_at(state)
            ends = _session_devices(edge_id)
            if state.get("status") != "up" and (side is None or ends is None):
                continue
            current = now.get(edge_id)
            if current is None:
                if still_adjacent & set(_adjacency_ends(edge_id)):
                    continue
                if side is None:
                    devices = {part for part in edge_id.split(":") if part in known}
                    if len(devices) == 2 and devices <= answering:
                        rows.append({"protocol": role, "edge_id": edge_id,
                                     "now": "not reported", "states": ""})
                    continue
                at = 0 if side == "local" else 1
                if ends[at] not in answering or listed.intersection(_adjacency_ends(edge_id)[at:at + 1]):
                    continue
                row = {"protocol": role, "edge_id": edge_id, "now": "not reported", "states": ""}
                rows.append(row if set(ends) <= answering else row | {"answered": ends[at]})
            elif current.get("status") in {"down", "degraded", "unidirectional"}:
                # Read from neither end (an expected adjacency both routers omit): its detail says why.
                unread = {current.get("local"), current.get("remote")} <= {None, "", "unknown"}
                states = (str(current["detail"]) if unread and current.get("detail")
                          else f"{current.get('local')} / {current.get('remote')}")
                # Two routers that see an adjacency differently: it is down, whatever each lists.
                status = "down" if current["status"] == "unidirectional" else str(current["status"])
                rows.append({"protocol": role, "edge_id": edge_id, "now": status, "states": states})
            elif side is not None and current.get(side) not in {"up", "established", None, "", "unknown"}:
                # A one-sided adjacency is recorded unknown whatever the end that was read lists.
                rows.append({"protocol": role, "edge_id": edge_id, "now": "down",
                             "states": f"{current.get('local')} / {current.get('remote')}"})
    return rows


def _routing_sessions_regained(older: dict[str, Any], newer: dict[str, Any]) -> list[dict[str, str]]:
    """Sessions and adjacencies not up in the older run that the newer one reads up.

    The mirror of ``_routing_sessions_lost``. Not up means the older run
    recorded it down, or it was absent although its devices answered that
    run's query in a full scan. Up at the one end that was read is held to
    that end: absent then although it answered, and the row names it.
    """
    known = set(older.get("device_names") or []) | set(newer.get("device_names") or [])
    rows: list[dict[str, str]] = []
    for role, layer in _ROUTING_LAYERS.items():
        before, now = _routing_edges(older, role), _routing_edges(newer, role)
        if before is None or not now:
            continue
        answered = _devices_answering(older, layer)
        # Parallel links are also recorded under ids that pair one link's end with another's.
        was_adjacent = {
            end for old_id, old_state in before.items() if old_state.get("status") == "up"
            for end in _adjacency_ends(old_id)
        } if role == "isis" else set()
        listed = {end for old_id in before for end in _adjacency_ends(old_id)}
        for edge_id, current in sorted(now.items()):
            side = None if current.get("status") == "up" else _seen_up_at(current)
            ends = _session_devices(edge_id)
            if current.get("status") != "up" and (side is None or ends is None):
                continue
            state = before.get(edge_id)
            if state is not None:
                sides = {state.get("local"), state.get("remote")}
                # A one-sided adjacency is recorded unknown whatever the end that was read lists.
                if not (state.get("status") in {"down", "degraded", "unidirectional"}
                        or (state.get("status") == "unknown" and not sides <= {None, "", "unknown"}
                            and not sides & {"up", "established"})):
                    continue
            elif was_adjacent & set(_adjacency_ends(edge_id)):
                continue
            elif side is None:
                devices = {part for part in edge_id.split(":") if part in known}
                if len(devices) != 2 or not devices <= answered:
                    continue
            else:
                at = 0 if side == "local" else 1
                if ends[at] not in answered or listed.intersection(_adjacency_ends(edge_id)[at:at + 1]):
                    continue
            if side is None:
                rows.append({"protocol": role, "edge_id": edge_id, "now": "up",
                             "states": f"{current.get('local')} / {current.get('remote')}"})
            else:
                rows.append({"protocol": role, "edge_id": edge_id, "now": str(current[side]), "states": "",
                             "up_at": ends[0 if side == "local" else 1]})
    return rows


def _run_start(case: dict[str, Any]) -> datetime | None:
    """When a scan began, from its id (``20261029T043006Z``)."""
    try:
        return datetime.strptime(str(case.get("run_id") or ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _adjacency_change_times(case: dict[str, Any]) -> dict[str, str]:
    """IS-IS adjacency id -> when it last changed state, as the scan read it."""
    found: dict[str, str] = {}
    for ev in case.get("evidence") or []:
        if not isinstance(ev, dict) or ev.get("kind") != "spine" or ev.get("role") != "isis":
            continue
        for edge in (ev.get("payload") or {}).get("operational_edges") or []:
            if isinstance(edge, dict) and edge.get("id") and edge.get("changed_at"):
                found[str(edge["id"])] = str(edge["changed_at"])
    return found


def adjacency_change_outcomes(older: dict[str, Any], newer: dict[str, Any]) -> dict[str, bool]:
    """IS-IS adjacency id -> True when both runs read it up and it last changed state after the older
    run began, False when both read it up and it changed before.

    Adjacencies not up in both runs, or with no change time read, are left out; so is everything
    when the older run's start is unknown.
    """
    began = _run_start(older)
    before, now = _routing_edges(older, "isis"), _routing_edges(newer, "isis")
    if began is None or not before or not now:
        return {}
    up = lambda state: state.get("status") == "up" or _seen_up_at(state) is not None
    changed = _adjacency_change_times(newer)
    return {edge_id: datetime.fromisoformat(changed[edge_id]) > began
            for edge_id, state in now.items() if edge_id in changed and up(state) and up(before.get(edge_id) or {})}


def _adjacencies_changed_between(older: dict[str, Any], newer: dict[str, Any]) -> list[dict[str, str]]:
    """IS-IS adjacencies both runs read up that last changed state after the older run began.

    Up when the older run read it, so a later change means it went down and
    came back since, unless the change fell in the minutes before that read.
    """
    changed = _adjacency_change_times(newer)
    return [{"protocol": "isis", "edge_id": edge_id, "changed_at": changed[edge_id]}
            for edge_id, moved in sorted(adjacency_change_outcomes(older, newer).items()) if moved]


def _lanes(case: dict[str, Any]) -> tuple[dict[str, tuple[str, str, Any, Any]], set[tuple[str, str]]]:
    """From the run's concluded port investigations: service -> (device, lane, time since the
    lane's last flap, when that flap was if read to the second), and the (device, lane) pairs
    found down (those with that time)."""
    on_lane: dict[str, tuple[str, str, Any, Any]] = {}
    down: set[tuple[str, str]] = set()
    for ev in case.get("evidence") or []:
        payload = ev.get("payload") if isinstance(ev, dict) and ev.get("kind") == "port_investigation" else None
        if not isinstance(payload, dict) or not payload.get("concluded"):
            continue
        device, ages = str(payload.get("device") or ""), payload.get("link_ages") or {}
        flapped = payload.get("link_flapped_at") or {}
        down.update((device, str(link)) for link in ages)
        for link, names in (payload.get("links") or {}).items():
            for name in names or []:
                on_lane[str(name)] = (device, str(link), ages.get(link), flapped.get(link))
    return on_lane, down


def _lane_note(name: str, now: dict[str, tuple[str, str, Any, Any]], down_before: set[tuple[str, str]]) -> str | None:
    """Where a newly detected fault sits: a new service on a lane already down is not a new outage."""
    if name not in now:
        return None
    device, lane, age, flapped_at = now[name]
    # An age counts from the investigation's read, not from when the report is read.
    when = (f", last flapped {_utc_minute(flapped_at)}" if flapped_at
            else ", never came up" if age == "never"
            else f", last flapped {age} ago" if age else "")
    return (f"on {device} {lane}" + when
            + ("; that lane was down in the previous run too" if (device, lane) in down_before else ""))


def _fixed_checks(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each listed service's saved fixed-check results, by service name."""
    out: dict[str, dict[str, Any]] = {}
    for extra in _service_extras(case):
        for key, record in (extra.get("services") or {}).items():
            if isinstance(record, dict) and isinstance(record.get("basic_checks"), dict):
                out[str(record.get("name") or str(key).split("/", 1)[-1]).strip()] = record["basic_checks"]
    return out


def _observed(check: dict[str, Any]) -> str:
    """A check's observation as text; some checks record a dict (effective_evpn, xconnect)."""
    observation = check.get("observation")
    return observation if isinstance(observation, str) else json.dumps(observation, sort_keys=True, default=str)


def _earlier_check_note(then: dict[str, Any] | None, now: dict[str, Any] | None) -> str | None:
    """What the previous scan's fixed check passed on the circuits this scan's check does not pass.

    Only results this scan does not repeat are named. It is an observation for the reader,
    not proof of when the fault began.
    """
    from diagnostic_mas.operational_checks.probe import attachments

    if not then or not now:
        return None
    checks_now = [c for c in now.get("checks") or [] if isinstance(c, dict)]
    passing_now = {(c.get("device"), c.get("check"), _observed(c)) for c in checks_now if c.get("status") == "pass"}
    found: dict[str, list[str]] = {}
    for endpoint in attachments(now.get("intent") or {}):
        device = endpoint["device"]
        # The circuit's own name, not one it begins: FH0/0/0/12.2385 is not FH0/0/0/12.23850.
        mentions = re.compile(rf"(?<![\w/.]){re.escape(endpoint['ac'])}(?![\w/]|\.\d)").search
        if not any(c.get("device") == device and c.get("status") != "pass" and mentions(_observed(c))
                   for c in checks_now):
            continue
        for check in then.get("checks") or []:
            if (isinstance(check, dict) and check.get("device") == device and check.get("status") == "pass"
                    and mentions(_observed(check))
                    and (device, check.get("check"), _observed(check)) not in passing_now):
                item = f"{check.get('check')} ({_observed(check)})"
                if item not in found.setdefault(device, []):
                    found[device].append(item)
    parts = [f"{device}: " + ", ".join(items[:3]) + (f", and {len(items) - 3} more" if len(items) > 3 else "")
             for device, items in found.items() if items]
    return "the previous scan's fixed check passed on " + "; ".join(parts) if parts else None


def _interface_states(case: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """(device, interface) -> recorded state, from the run's interface collection."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for extra in _service_extras(case):
        for edge in extra.get("physical_operational_edges") or []:
            device, _, interface = str((edge or {}).get("id") or "").removeprefix("if:").partition(":")
            if device and interface:
                out[device, interface] = edge.get("state") or {}
    return out


def _services_on_ports(case: dict[str, Any]) -> dict[tuple[str, str], set[str]]:
    """(device, port in its long spelling) -> the services attached there in that run."""
    from diagnostic_mas.operational_checks.probe import attachments
    from diagnostic_mas.port_counters import _parent_port

    out: dict[tuple[str, str], set[str]] = {}
    for extra in _service_extras(case):
        for key, record in (extra.get("services") or {}).items():
            if not isinstance(record, dict):
                continue
            name = str(record.get("name") or str(key).split("/", 1)[-1]).strip()
            for end in attachments((record.get("basic_checks") or {}).get("intent") or {}):
                out.setdefault((end["device"], _parent_port(end["ac"])), set()).add(name)
    return out


def _interfaces_lost(older: dict[str, Any], newer: dict[str, Any]) -> list[dict[str, Any]]:
    """Interfaces up in the older run that the newer run recorded as down.

    Only a recorded down or admin-down state counts. An interface missing from
    the newer list, or without a collected state, is not evidence of a fault.
    ``services_deleted`` counts the older run's services on the port when every
    one of them is gone from the newer run and no newer service uses the port;
    otherwise it is 0.
    ``services_down_before`` counts those the older run itself found Down: the
    port can fail after that run read it up, and the run then reports the loss.
    """
    from diagnostic_mas.port_counters import _parent_port

    gone = set(_services_no_longer_present(older, newer))
    riders = _services_on_ports(older)
    riders_now = _services_on_ports(newer)
    down_before = {name for name, finding in _service_findings(older).items()
                   if finding.get("status") == "down" and finding.get("complete") is not False}
    now = _interface_states(newer)
    lost = {
        key: str(now[key]["oper"])
        for key, state in _interface_states(older).items()
        if state.get("oper") == "up" and (now.get(key) or {}).get("oper") in {"down", "admin-down"}
    }
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for (device, interface), state in sorted(lost.items()):
        parent, dot, unit = interface.rpartition(".")
        if dot and unit.isdigit() and (device, parent) in lost:
            rows[device, parent]["sub_interfaces"] += 1   # the parent sorts first
        else:
            port = (device, _parent_port(interface))
            on_port = riders.get(port, set())
            deleted = on_port and on_port <= gone and not riders_now.get(port)
            rows[device, interface] = {"device": device, "interface": interface,
                                       "now": state, "sub_interfaces": 0,
                                       "services_deleted": len(on_port) if deleted else 0,
                                       "services_down_before": len(on_port & down_before),
                                       "services_on_port": len(on_port)}
    return list(rows.values())


def _interface_lines(rows: list[dict[str, Any]], *, per_device: int = 6) -> list[str]:
    by_device: dict[str, list[str]] = {}
    for row in rows:
        n, deleted = row["sub_interfaces"], row.get("services_deleted") or 0
        down, riders = row.get("services_down_before") or 0, row.get("services_on_port") or 0
        note = (" (admin-down)" if row["now"] == "admin-down" else "") + (
            f" (+{n} sub-interface{'s' if n != 1 else ''})" if n else "") + (
            " (its service was deleted)" if deleted == 1
            else f" (its {deleted} services were deleted)" if deleted else "") + (
            "" if not down
            else " (its service was already Down in the previous run)" if down == riders == 1
            else f" (its {down} services were already Down in the previous run)" if down == riders
            else f" ({down} of its {riders} services {'was' if down == 1 else 'were'} "
                 "already Down in the previous run)")
        by_device.setdefault(row["device"], []).append(row["interface"] + note)
    return [
        f"`{device}`: " + ", ".join(names[:per_device])
        + (f", +{len(names) - per_device} more" if len(names) > per_device else "")
        for device, names in by_device.items()
    ]


def _cleared_line(row: dict[str, str]) -> str:
    label = f"{row['service_type']}/{row['service']}" if row.get("service_type") else row["service"]
    return (f"`{label}` — passes the checks this run "
            f"(was {row['was']} in run `{row['last_observed_run_id']}`)")


def _adjacency_change_line(row: dict[str, str], history: dict[str, dict[str, Any]] | None = None) -> str:
    from diagnostic_mas.item_history import change_key, clause_for

    clause = clause_for(history, change_key(row["edge_id"]), "changed state")
    return f"{_session_label(row)} — last changed {_utc_minute(row['changed_at'])}" + (f"; {clause}" if clause else "")


def _utc_minute(stamp: str) -> str:
    """An ISO time as the report gives it: ``29 Oct 22:47 UTC``."""
    when = datetime.fromisoformat(stamp).astimezone(timezone.utc)
    return f"{when.day} {when:%b %H:%M} UTC"


def _session_label(row: dict[str, str]) -> str:
    parts = row["edge_id"].split(":")
    label = f"`{row['edge_id']}`"
    if len(parts) == 3 and row["protocol"] == "bgp-outside":
        label = f"BGP `{parts[1]}` → {parts[2]} (outside peer, AS {row.get('remote_as')})"
    elif len(parts) == 5 and row["protocol"] == "bgp":
        _, addr_a, addr_b, dev_a, dev_b = parts
        label = f"BGP `{dev_a}` {addr_a} ↔ `{dev_b}` {addr_b}"
    elif len(parts) == 5 and row["protocol"] == "isis":
        _, dev_a, if_a, dev_b, if_b = parts
        # A far end that was not read has no interface ("?").
        end = lambda device, interface: f"`{device}`" + ("" if interface == "?" else f" {interface}")
        label = f"IS-IS {end(dev_a, if_a)} ↔ {end(dev_b, if_b)}"
    return label


def _routing_session_line(row: dict[str, str]) -> str:
    label = _session_label(row)
    if row.get("up_at"):
        other = next(device for device in _session_devices(row["edge_id"]) or () if device != row["up_at"])
        return f"{label} — {row['now']} at `{row['up_at']}`; `{other}` was not read"
    if row["now"] == "not reported" and row.get("answered"):
        other = next(device for device in _session_devices(row["edge_id"]) or () if device != row["answered"])
        return (f"{label} — not reported this run by `{row['answered']}`, which listed it up last run; "
                f"`{other}` was not read")
    if row["now"] == "not reported":
        return f"{label} — not reported this run; both devices answered"
    return f"{label} — {row['now']} ({row['states']})"


def _issue_subject(issue: dict[str, Any]) -> str:
    edge = issue.get("edge_id")
    if isinstance(edge, str) and edge.strip():
        return edge.strip()
    devices = issue.get("devices")
    if isinstance(devices, list) and devices:
        return ",".join(str(d) for d in devices)
    return str(issue.get("code") or "unknown")


def _issue_line(issue: dict[str, Any]) -> str:
    subject = _issue_subject(issue)
    code = str(issue.get("code") or "?")
    status = str(issue.get("status") or "?")
    msg = str(issue.get("message") or "").strip()
    layer = str(issue.get("layer") or "")
    head = f"{subject} [{code}] ({status})"
    if layer:
        head = f"{subject} [{layer}/{code}] ({status})"
    if msg:
        return f"{head}: {msg}"
    return head


def _subject_in_coverage(subject: str, coverage: dict[str, str]) -> bool:
    if not coverage:
        return True  # unknown scope — do not treat as out-of-scope
    if subject in coverage:
        return True
    # device-keyed problems: any coverage is unrelated; treat as in-scope
    if subject.startswith("devices:") or "," in subject:
        return True
    return False


def _quarantined_devices(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """device_live_unreachable issues keyed by device name."""
    out: dict[str, dict[str, Any]] = {}
    for issue in case.get("issues") or []:
        if not isinstance(issue, dict) or not is_problem(issue):
            continue
        if str(issue.get("code") or "") != "device_live_unreachable":
            continue
        devices = issue.get("devices") or []
        if isinstance(devices, list) and devices:
            name = str(devices[0]).strip()
        else:
            name = _issue_subject(issue)
        if name:
            out[name] = issue
    return out


def _service_dataplane_map(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map service name → {status, complete, service_type} from diagnoses/evidence."""
    out: dict[str, dict[str, Any]] = {}

    def _put(
        name: str,
        *,
        status: str,
        complete: bool = True,
        service_type: str | None = None,
    ) -> None:
        key = str(name or "").strip()
        if not key:
            return
        st = str(status or "").lower()
        if not st:
            return
        prev = out.get(key)
        # Prefer incomplete / fault over a later ok when both exist.
        if prev is not None:
            if prev.get("complete") is False and complete:
                return
            if str(prev.get("status") or "") in _FAULT_DP and st in _OK_DP:
                return
        row: dict[str, Any] = {"status": st, "complete": complete}
        if service_type:
            row["service_type"] = service_type
        out[key] = row

    for dx in case.get("diagnoses") or []:
        if dx["kind"] != "dataplane":
            continue
        subject = dx["subject"]
        _put(subject["name"], status=dx["status"], complete=dx["complete"],
             service_type=subject["service_type"] or None)

    for ev in case.get("evidence") or []:
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("kind") or "")
        if kind not in {"dataplane_finding", "dataplane_incomplete"}:
            continue
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        name = payload.get("name") or payload.get("service_name")
        if not name:
            continue
        status = payload.get("dataplane_status") or ""
        complete = kind != "dataplane_incomplete" and payload.get("complete", True)
        _put(
            str(name),
            status=str(status),
            complete=False if complete is False else True,
            service_type=str(payload.get("service_type") or "") or None,
        )
    return out


def _passing_this_run(case: dict[str, Any]) -> set[str]:
    """Services whose final status in this run is OpUp, by fixed check or LLM."""
    from diagnostic_mas.service_final_status import assessment

    digs = {dx["subject"]["name"]: dx for dx in case.get("diagnoses") or [] if dx["kind"] == "dataplane"}
    return {
        str(record["name"])
        for extra in _service_extras(case)
        for record in (extra.get("services") or {}).values()
        if isinstance(record, dict) and record.get("name")
        and assessment(record, digs.get(str(record["name"])))[0] == "up"
    }


def _judged_services(case: dict[str, Any]) -> dict[str, tuple[str, str, Any]]:
    """Service name -> (type, final status, what its fixed check read), for investigated services."""
    from diagnostic_mas.service_final_status import assessment

    digs = {dx["subject"]["name"]: dx for dx in case.get("diagnoses") or [] if dx["kind"] == "dataplane"}
    out: dict[str, tuple[str, str, Any]] = {}
    for extra in _service_extras(case):
        for record in (extra.get("services") or {}).values():
            basic = record.get("basic_checks") if isinstance(record, dict) else None
            if not isinstance(basic, dict) or not record.get("name") or basic.get("status") in {None, "not_checked"}:
                continue
            read = (basic.get("status"), sorted(
                json.dumps([c.get("check"), c.get("status"), c.get("device"), c.get("observation")],
                           sort_keys=True, default=str)
                for c in basic.get("checks") or [] if isinstance(c, dict)))
            name = str(record["name"])
            # Only an investigation can give a second opinion; a scan that ran none gave no verdict.
            if name in digs:
                out[name] = (str(record.get("service_type") or ""), assessment(record, digs[name])[0], read)
    return out


def _status_changed_on_unchanged_checks(older: dict[str, Any], newer: dict[str, Any]) -> list[dict[str, str]]:
    """Services investigated in both scans whose final status differs while the fixed check read the same."""
    before, now = _judged_services(older), _judged_services(newer)
    return [
        {"service": name, "service_type": kind, "was": before[name][1], "now": status}
        for name, (kind, status, read) in sorted(now.items())
        if name in before and before[name][1] != status and before[name][2] == read
    ]


def _status_change_line(row: dict[str, str]) -> str:
    from diagnostic_mas.service_final_status import LABELS, STATUSES

    label = dict(zip(STATUSES, LABELS))
    name = f"{row['service_type']}/{row['service']}" if row.get("service_type") else row["service"]
    return f"`{name}` — {label.get(row['was'], row['was'])} → {label.get(row['now'], row['now'])}"


# An end must have received at least this many prefixes before a drop is a lead: smaller
# tables change whenever an owner adds or deletes a service.
_PREFIX_FLOOR = 1000


def _prefix_drop(was: Any, now_state: Any, before: Any, now: Any, up_before: Any, up_now: Any) -> dict[str, Any] | None:
    """An end Established in both scans that now receives fewer than half the prefixes it did; None otherwise."""
    from diagnostic_mas.port_counters import _age_seconds

    if not (was == now_state == "established" and isinstance(before, int) and isinstance(now, int)):
        return None
    if before < _PREFIX_FLOOR or now * 2 >= before:
        return None
    age_before, age_now = _age_seconds(up_before), _age_seconds(up_now)
    # A shorter Up/Down than in the previous scan: the session went down and came back in between.
    reset = age_before is not None and age_now is not None and age_now < age_before
    return {"before": before, "now": now, "up_down": up_now, "reset": reset}


def _sessions_receiving_fewer_prefixes(older: dict[str, Any], newer: dict[str, Any]) -> list[dict[str, Any]]:
    """BGP sessions still Established whose end now receives far fewer prefixes, largest drop first.

    A lead on a working session, like a link gaining errors: no service status rests on it.
    A session that is no longer Established is reported as lost instead.
    """
    rows: list[dict[str, Any]] = []
    then_edges = _routing_edges(older, "bgp") or {}
    for edge_id, state in (_routing_edges(newer, "bgp") or {}).items():
        then, devices = then_edges.get(edge_id), _session_devices(edge_id)
        if not then or not devices:
            continue
        for side, device, peer in (("local", *devices), ("remote", *devices[::-1])):
            found = _prefix_drop(then.get(side), state.get(side),
                                 (then.get("prefixes") or {}).get(side), (state.get("prefixes") or {}).get(side),
                                 (then.get("up_down") or {}).get(side), (state.get("up_down") or {}).get(side))
            if found:
                rows.append({"device": device, "peer": peer, **found})
    then_rows = _unmatched_rows(older) or {}
    for (device, address), row in (_unmatched_rows(newer) or {}).items():
        then = then_rows.get((device, address))
        found = then and _prefix_drop(then.get("state"), row.get("state"), then.get("prefixes"), row.get("prefixes"),
                                      then.get("up_down"), row.get("up_down"))
        if found:
            rows.append({"device": device, "address": address, "remote_as": row.get("remote_as"),
                         "outside": bool(row.get("outside")), **found})
    return sorted(rows, key=lambda row: (row["now"] - row["before"], row["device"]))


def prefix_drop_line(row: dict[str, Any]) -> str:
    """``wash-data-sw`` from ``star-data-sw``: 515,490 → 0 prefixes (−100%); session up 34w3d."""
    if row.get("peer"):
        source = f"`{row['peer']}`"
    else:
        source = (f"{'outside peer' if row.get('outside') else 'neighbor'} {row.get('address')}"
                  + (f" (AS {row['remote_as']})" if row.get("remote_as") else ""))
    drop = f"{(row['before'] - row['now']) / row['before'] * 100:.1f}".rstrip("0").rstrip(".")
    up = (f"session re-established since the previous scan (up {row['up_down']})" if row.get("reset")
          else f"session up {row['up_down']}" if row.get("up_down") else "")
    return (f"`{row['device']}` from {source}: {row['before']:,} → {row['now']:,} prefixes (−{drop}%)"
            + (f"; {up}" if up else ""))


def _links_gaining_errors(older: dict[str, Any], newer: dict[str, Any]) -> list[dict[str, Any]]:
    from diagnostic_mas.port_counters import links_gaining_errors

    return links_gaining_errors(older, newer)


def _service_issue_faults(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Service-layer problem issues keyed by edge_id / subject."""
    out: dict[str, dict[str, Any]] = {}
    for issue in case.get("issues") or []:
        if not isinstance(issue, dict) or not is_problem(issue):
            continue
        if str(issue.get("layer") or "") != "services":
            continue
        subject = _issue_subject(issue)
        if subject:
            out[subject] = issue
    return out



def _service_findings(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """What one run found per service: collection-reported faults, overridden by completed diagnostics."""
    findings = {}
    for name, issue in _service_issue_faults(case).items():
        code = issue.get("code")
        if code in {"service_down", "service_degraded"}:
            findings[name] = {
                "status": code.removeprefix("service_"),
                "complete": True,
                "cause": issue.get("message", ""),
                "source": "collection",
            }
    findings.update(_service_dataplane_map(case))
    return findings


def service_fault_history(
    older: dict[str, Any], newer: dict[str, Any],
    *, previous_run_id: str | None = None, run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Carry unresolved faults through sampling gaps without asserting current health.

    A historical fault is cleared by a pass in the current run: a complete
    dataplane pass, or the service's own operational checks passing.
    Baseline sync, a narrower scan, and incomplete digs cannot clear it.
    A fault ends without being cleared when a full scan no longer lists
    the service: there is nothing left to recheck.
    """
    older, newer = _shaped(older), _shaped(newer)
    history = {
        str(row["service"]): dict(row)
        for row in older.get("last_known_service_faults") or []
        if isinstance(row, dict) and row.get("service")
    }
    for snapshot, observed_run in (
        (older, previous_run_id or older.get("run_id")),
        (newer, run_id or newer.get("run_id")),
    ):
        kinds = _service_inventory(snapshot)
        findings = _service_findings(snapshot)
        for name, finding in findings.items():
            st = finding.get("status")
            if finding.get("complete") is False:
                continue
            if st in _OK_DP:
                history.pop(name, None)
            elif st in _FAULT_DP:
                diagnosis = next((
                    d for d in snapshot.get("diagnoses") or []
                    if d["kind"] == "dataplane" and d["subject"]["name"] == name and d["status"] == st
                ), {})
                # A row carried without a first-seen scan is dated from the saved scans later.
                earlier = history.get(name)
                first = observed_run if earlier is None else earlier.get("first_observed_run_id")
                history[name] = {
                    "service": name, "status": st,
                    "service_type": finding.get("service_type") or kinds.get(name, ""),
                    "first_observed_run_id": first or None,
                    "first_observed_exact": bool(first) and (earlier is None or bool(earlier.get("first_observed_exact"))),
                    "last_observed_run_id": observed_run or "unknown",
                    "cause": diagnosis.get("cause") or finding.get("cause") or "",
                    "observed": diagnosis.get("observed") or "",
                    "source": diagnosis.get("source") or finding.get("source") or "recorded finding",
                    "evidence_ids": list(diagnosis.get("evidence_ids") or []),
                }
    collected = _collected_types(newer)
    if collected is not None:
        now = _service_inventory(newer)
        known_types = _service_inventory(older)
        for name in [n for n, row in history.items() if n not in now
                     and (row.get("service_type") or known_types.get(n)) in collected]:
            del history[name]
    for name in _passing_this_run(newer) & history.keys():
        del history[name]
    current_dp = _service_dataplane_map(newer)
    current_issues = _service_issue_faults(newer)
    for name, row in history.items():
        current = current_dp.get(name, {})
        confirmed = (
            current.get("status") in _FAULT_DP
            and current.get("complete") is not False
        ) or (
            not current and current_issues.get(name, {}).get("code")
            in {"service_down", "service_degraded"}
        )
        row["rechecked"] = bool(confirmed)
        row["verification"] = (
            "fault observed this run" if confirmed else
            "recheck inconclusive; last-known fault unresolved" if current else
            "not rechecked; last-known fault unresolved"
        )
    return sorted(history.values(), key=lambda r: r["service"])


def first_seen_from_saved_scans(rows: list[dict[str, Any]], runs_dir: Path | str, *, before: str) -> None:
    """Date the faults that were carried without a first-seen scan, from the saved scans.

    Walks the saved scans older than ``before``, newest first, for as long as
    each still lists the fault. When the oldest readable scan still lists it,
    the fault may be older than the history, so the date is not exact. This
    reads how long a fault has been listed, never whether it is still there.
    """
    pending = {str(r["service"]): r for r in rows if isinstance(r, dict) and not r.get("first_observed_run_id")}
    for row in pending.values():
        row.update(first_observed_run_id=None, first_observed_exact=False)
    try:
        saved = sorted((d.name for d in Path(runs_dir).iterdir() if d.is_dir() and d.name < str(before)), reverse=True)
    except OSError:
        return
    for run in saved:
        if not pending:
            return
        try:
            listed = load_case_dict(Path(runs_dir) / run / "case.json").get("last_known_service_faults")
        except (OSError, ValueError):
            return
        if not isinstance(listed, list):
            return
        then = {str(r.get("service")): r for r in listed if isinstance(r, dict)}
        for name in list(pending):
            seen = then.get(name)
            if seen is None:
                # Not open in this scan: it began in the scan after, if one was found.
                row = pending.pop(name)
                row["first_observed_exact"] = bool(row["first_observed_run_id"])
            elif seen.get("first_observed_run_id"):
                pending.pop(name).update(first_observed_run_id=seen["first_observed_run_id"],
                                         first_observed_exact=bool(seen.get("first_observed_exact")))
            else:
                pending[name]["first_observed_run_id"] = run


def format_last_known_faults(rows: list[dict[str, Any]]) -> list[str]:
    pending = [r for r in rows if not r.get("rechecked")]
    if not pending:
        return []
    lines = ["**Last-known service faults — not cleared:**", ""]
    for row in pending:
        lines.append(
            f"- `{row['service']}` — last known {row['status']} "
            f"(run `{row.get('last_observed_run_id', 'unknown')}`); "
            f"{row.get('verification', 'not rechecked')}. "
            f"{row.get('cause') or ''}".rstrip()
        )
    lines.append("")
    return lines

@dataclass(frozen=True)
class _ScanPair:
    """The two scans compute_case_delta compares, with the lookups its parts share."""

    older: dict[str, Any]
    newer: dict[str, Any]
    old_probs: dict[str, dict[str, Any]]
    new_probs: dict[str, dict[str, Any]]
    old_cov: dict[str, str]
    new_cov: dict[str, str]
    old_q: dict[str, dict[str, Any]]
    new_q: dict[str, dict[str, Any]]
    live_verified: set[str]
    old_dp: dict[str, dict[str, Any]]
    new_dp: dict[str, dict[str, Any]]
    # Service faults carried into the older scan, by service.
    old_history: dict[str, dict[str, Any]]
    # Services a full newer scan no longer lists, with their type.
    gone: dict[str, str]


class CaseDelta(TypedDict):
    """What changed between the previous saved scan and this one: every key, written down once.

    ``compute_case_delta`` sets all but the last two, which the scan adds after it. A list is empty,
    never missing, when nothing changed; hand-made deltas, as in tests, may still leave keys out.
    """
    # The issue lists: problems new this scan, read clear, still open, and gone this scan without
    # being confirmed clear (the last is read by nothing).
    new_problems: list[Issue]
    recovered: list[Issue]
    persistent: list[Issue]
    out_of_scope: list[Issue]
    # Previous services whose coverage label changed, "no longer present" included.
    coverage_changes: list[dict[str, str]]
    # Services faulty now that were not before: a detection, not a proven regression.
    newly_failed_services: list[dict[str, str]]
    # The service-fault ledger, carried through scans that did not recheck a fault.
    last_known_service_faults: list[dict[str, Any]]
    # Devices unreadable last scan that this scan read live.
    recovered_devices: list[str]
    # Coverage lost, investigations that newly did not finish, and devices newly unreadable.
    newly_missing_evidence: list[dict[str, str]]
    # Ledger faults this scan ended: the service now passes, or a full scan no longer lists it.
    service_faults_cleared: list[dict[str, str]]
    service_faults_no_longer_present: list[dict[str, str]]
    # Services investigated in both scans whose final status changed while the fixed check read the same.
    status_changed_on_unchanged_checks: list[dict[str, str]]
    # Leads on working links and sessions; no service status rests on them.
    links_gaining_errors: list[dict[str, Any]]
    sessions_receiving_fewer_prefixes: list[dict[str, Any]]
    # Devices the previous scan recorded NSO failing to authenticate to, to tell new ones apart.
    authentication_failed_before: list[str]
    # BGP sessions and IS-IS adjacencies up last scan and not now (outside BGP included), and the reverse.
    routing_sessions_lost: list[dict[str, Any]]
    routing_sessions_regained: list[dict[str, str]]
    # IS-IS adjacencies up in both scans that last changed state after the previous scan began.
    adjacencies_changed_between_scans: list[dict[str, str]]
    # Interfaces up last scan that this scan recorded down.
    interfaces_lost: list[dict[str, Any]]
    # Services a full scan no longer lists: deleted by their owners, neither a fault nor a gap.
    services_no_longer_present: list[dict[str, str]]
    # When the two scans focused on different devices, or only one recorded service coverage.
    notes: list[str]
    # Added by the scan: how long routing faults, error links and IS-IS changes have been seen,
    # and the open faults grouped by the port or device that explains them.
    item_history: NotRequired[dict[str, dict[str, Any]]]
    open_faults: NotRequired[list[dict[str, Any]]]


def compute_case_delta(
    older: dict[str, Any], newer: dict[str, Any]
) -> CaseDelta:
    """Diff two case dicts into operational change buckets.

    Buckets operators care about most:
    - newly failed services
    - recovered devices
    - newly missing evidence
    - persistent problems
    Plus legacy issue lists (new_problems / recovered / out_of_scope) and
    coverage_changes.
    """
    older, newer = _shaped(older), _shaped(newer)
    pair = _ScanPair(
        older=older, newer=newer,
        old_probs=problem_map(older), new_probs=problem_map(newer),
        old_cov=coverage_map(older), new_cov=coverage_map(newer),
        old_q=_quarantined_devices(older), new_q=_quarantined_devices(newer),
        live_verified=set(newer.get("live_verified_devices") or []),
        old_dp=_service_dataplane_map(older), new_dp=_service_dataplane_map(newer),
        old_history={r["service"]: r for r in service_fault_history(older, {})},
        gone=_services_no_longer_present(older, newer),
    )
    history = service_fault_history(older, newer)
    new_problems, recovered, persistent, out_of_scope = _sorted_issues(
        pair, pending_services={r["service"] for r in history if not r["rechecked"]})
    coverage_changes, coverage_gaps = _coverage_changes(pair)

    return {
        "new_problems": new_problems,
        "recovered": recovered,
        "persistent": persistent,
        "out_of_scope": out_of_scope,
        "coverage_changes": coverage_changes,
        # Keep the legacy JSON key for consumers; no claim of failure onset.
        "newly_failed_services": _newly_failed_services(pair),
        "last_known_service_faults": history,
        "recovered_devices": sorted(d for d in pair.old_q if d not in pair.new_q and d in pair.live_verified),
        "newly_missing_evidence": coverage_gaps + _new_collection_gaps(pair),
        "service_faults_cleared": [
            _ended_fault(name, row) for name, row in sorted(pair.old_history.items())
            if name in _passing_this_run(newer) and name not in pair.gone
        ],
        # Ended without a recheck: a full scan no longer lists the service, so its owner deleted it.
        "service_faults_no_longer_present": [
            _ended_fault(name, row) for name, row in sorted(pair.old_history.items()) if name in pair.gone
        ],
        "status_changed_on_unchanged_checks": _status_changed_on_unchanged_checks(older, newer),
        "links_gaining_errors": _links_gaining_errors(older, newer),
        # Devices the previous scan recorded NSO failing to authenticate to, to tell new ones apart.
        "authentication_failed_before": sorted(authentication_failures(older)),
        "sessions_receiving_fewer_prefixes": _sessions_receiving_fewer_prefixes(older, newer),
        "routing_sessions_lost": _routing_sessions_lost(older, newer) + _outside_sessions_lost(older, newer),
        "routing_sessions_regained": _routing_sessions_regained(older, newer),
        "adjacencies_changed_between_scans": _adjacencies_changed_between(older, newer),
        "interfaces_lost": _interfaces_lost(older, newer),
        "services_no_longer_present": [
            {"service": name, "service_type": kind,
             "previous_coverage": pair.old_cov.get(name, "")}
            for name, kind in sorted(pair.gone.items())
        ],
        "notes": _scope_notes(pair),
    }


def _ended_fault(name: str, row: dict[str, Any]) -> dict[str, Any]:
    """A last-known service fault that this scan ended, with the status it had when last read."""
    return {"service": name, "service_type": row.get("service_type") or "",
            "was": row.get("status") or "", "last_observed_run_id": row.get("last_observed_run_id") or "unknown"}


def _sorted_issues(
    pair: _ScanPair, *, pending_services: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """The problems of both scans as new, recovered, persistent and out of scope.

    A problem the older scan had and the newer one lacks is recovered only when the newer scan
    could have seen it again: not when it moved to the outside-peer list or its service is gone,
    not rechecked or outside the newer scan's coverage.
    """
    new_problems = [issue for key, issue in pair.new_probs.items() if key not in pair.old_probs]
    persistent = [issue for key, issue in pair.new_probs.items() if key in pair.old_probs]
    recovered: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, Any]] = []
    now_outside = {key for key, row in (_unmatched_rows(pair.newer) or {}).items() if row.get("outside")}
    for key, issue in pair.old_probs.items():
        if key in pair.new_probs:
            continue
        # Now listed as a session to another network's router: re-sorted, not recovered.
        if (issue.get("code") == "unknown_neighbor_address"
                and unmatched_neighbor_in(str(issue.get("message") or "")) in now_outside):
            continue
        subject = _issue_subject(issue)
        # Listed under services no longer present: neither recovered nor skipped.
        if str(issue.get("layer") or "") == "services" and subject in pair.gone:
            continue
        if issue.get("code") == "device_live_unreachable":
            devices = set(issue.get("devices") or [])
            if devices and devices <= pair.live_verified and not devices.intersection(pair.new_q):
                recovered.append(issue)
            else:
                out_of_scope.append(issue)
            continue
        # A baseline pass or an omitted service is not dataplane recovery.
        if str(issue.get("layer") or "") == "services" and subject in pending_services:
            out_of_scope.append(issue)
            continue
        # Narrower newer run: missing from newer coverage → not a recovery.
        if pair.new_cov and not _subject_in_coverage(subject, pair.new_cov):
            out_of_scope.append(issue)
        else:
            recovered.append(issue)
    return new_problems, recovered, persistent, out_of_scope


def _coverage_changes(pair: _ScanPair) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """How each older service's coverage changed, and the evidence gaps that leaves.

    A service no longer in NSO, or removed while the older scan ran, leaves no gap.
    """
    changes: list[dict[str, str]] = []
    gaps: list[dict[str, str]] = []
    new_cov = pair.new_cov
    reported_removed = _removed_during_scan(pair.older)
    for name, old_label in pair.old_cov.items():
        if name not in new_cov and name in reported_removed:
            continue
        if name not in new_cov and name in pair.gone:
            # No longer in NSO, not left unchecked: no evidence is missing.
            changes.append({"service": name, "from": old_label, "to": "no longer present",
                            "kind": "no_longer_present"})
        elif name not in new_cov:
            changes.append({"service": name, "from": old_label, "to": "not_checked", "kind": "not_checked"})
            if old_label in _CHECKED_COVERAGE:
                gaps.append({"kind": "coverage_gap", "subject": name, "detail": f"{old_label} → not_checked"})
        elif old_label in _CHECKED_COVERAGE and new_cov[name] == "budget_skipped":
            if old_label != "budget_skipped":
                changes.append({"service": name, "from": old_label, "to": "budget_skipped",
                                "kind": "budget_skipped"})
                gaps.append({"kind": "coverage_gap", "subject": name, "detail": f"{old_label} → budget_skipped"})
        elif old_label != new_cov[name]:
            changes.append({"service": name, "from": old_label, "to": new_cov[name], "kind": "label_change"})
    return changes, gaps


def _new_collection_gaps(pair: _ScanPair) -> list[dict[str, str]]:
    """Investigations that newly did not finish, then devices newly unreadable: evidence missing this run."""
    gaps: list[dict[str, str]] = []
    for name, row in pair.new_dp.items():
        if row.get("complete") is not False:
            continue
        prev = pair.old_dp.get(name)
        if prev is None or prev.get("complete") is not False:
            gaps.append({"kind": "incomplete_dig", "subject": name, "detail": "dataplane verification incomplete"})
    for device, issue in pair.new_q.items():
        if device in pair.old_q:
            continue
        msg = str(issue.get("message") or "").strip()
        gaps.append({"kind": "collection_gap", "subject": device, "detail": msg or "live MCP collection failed"})
    return gaps


def _newly_failed_services(pair: _ScanPair) -> list[dict[str, str]]:
    """Services faulty in the newer scan that were not before, each once: a detection, not a proven regression.

    Dataplane findings come first; a service problem adds a service only when neither scan's
    dataplane listed it. Sync or collection Unknowns are not faults. Each row says which lane it
    is on and what the previous scan's fixed check passed, when known.
    """
    failed: dict[str, dict[str, str]] = {}

    def _add(name: str, *, status: str, detail: str, service_type: str | None = None) -> None:
        key = str(name).strip()
        if not key or key in failed:
            return
        row: dict[str, str] = {
            "service": key,
            "status": status,
            "detail": detail,
            "classification": "newly_detected_fault",
            "regression_proven": False,
        }
        if service_type:
            row["service_type"] = service_type
        failed[key] = row

    new_services = _services_new_since(pair.older, pair.newer)
    for name, row in pair.new_dp.items():
        st = str(row.get("status") or "").lower()
        if st not in _FAULT_DP:
            continue
        prev = pair.old_history.get(name) or pair.old_dp.get(name)
        prev_st = str((prev or {}).get("status") or "").lower()
        service_type = str(row.get("service_type") or "") or None
        if prev is None or prev_st in _OK_DP or prev_st in {"", "unknown", "not_checked"}:
            _add(
                name,
                status=st,
                detail=(
                    f"dataplane {prev_st or 'unchecked'} → {st}"
                    if prev is not None
                    else f"dataplane={st}; service not in the previous scan"
                    if name in new_services
                    else f"dataplane={st} (new)"
                ),
                service_type=service_type,
            )
        elif prev_st != st and prev_st in _FAULT_DP:
            # severity change still operationally new
            _add(name, status=st, detail=f"dataplane {prev_st} → {st}", service_type=service_type)

    old_svc_faults = _service_issue_faults(pair.older)
    for name, issue in _service_issue_faults(pair.newer).items():
        if name in failed:
            continue
        if name in old_svc_faults or name in pair.old_history:
            continue
        # Skip pure collection/sync unknowns that are not service faults.
        code = str(issue.get("code") or "")
        if code in {"service_unknown", "service_sync_unknown"}:
            continue
        _add(
            name,
            status=str(issue.get("status") or "open"),
            detail=str(issue.get("message") or code or "new service problem"),
        )

    rows = list(failed.values())
    lanes_now, _ = _lanes(pair.newer)
    _, lanes_down_before = _lanes(pair.older)
    checks_then, checks_now = _fixed_checks(pair.older), _fixed_checks(pair.newer)
    for row in rows:
        for note in (_lane_note(row["service"], lanes_now, lanes_down_before),
                     _earlier_check_note(checks_then.get(row["service"]), checks_now.get(row["service"]))):
            if note:
                row["detail"] = f"{row['detail']}; {note}" if row.get("detail") else note
    return rows


def _scope_notes(pair: _ScanPair) -> list[str]:
    """Notes when the two scans focused on different devices or only one recorded service coverage."""
    focus_old = list(pair.older.get("focus_devices") or [])
    focus_new = list(pair.newer.get("focus_devices") or [])
    notes: list[str] = []
    if focus_old != focus_new and (focus_old or focus_new):
        notes.append(f"focus_devices differ: {focus_old or '(none)'} → {focus_new or '(none)'}")
    if pair.old_cov and not pair.new_cov:
        notes.append("newer case has no service_coverage (older runs may predate coverage persistence)")
    if pair.new_cov and not pair.old_cov:
        notes.append("older case has no service_coverage (older runs may predate coverage persistence)")
    return notes


def delta_has_operational_changes(delta: CaseDelta) -> bool:
    """True when any operator-priority bucket is non-empty."""
    if delta.get("last_known_service_faults"):
        return True
    if delta.get("newly_failed_services"):
        return True
    if delta.get("recovered_devices"):
        return True
    if delta.get("newly_missing_evidence"):
        return True
    if delta.get("routing_sessions_lost") or delta.get("interfaces_lost"):
        return True
    if delta.get("routing_sessions_regained") or delta.get("adjacencies_changed_between_scans"):
        return True
    if delta.get("status_changed_on_unchanged_checks") or delta.get("links_gaining_errors"):
        return True
    if delta.get("sessions_receiving_fewer_prefixes"):
        return True
    if delta.get("service_faults_cleared"):
        return True
    if delta.get("services_no_longer_present"):
        return True
    if delta.get("persistent"):
        return True
    if delta.get("new_problems") or delta.get("recovered"):
        return True
    if delta.get("coverage_changes"):
        return True
    return False


def format_changes_since_previous(
    delta: CaseDelta | None,
    *,
    previous_run_id: str | None = None,
    persistent_limit: int = 8,
) -> list[str]:
    """Compact operator section for the diagnostic report.

    Emphasizes new failures, recovered devices, newly missing evidence, and
    persistent problems — not unchanged PE-side observations.
    """
    from diagnostic_mas.item_history import routing_fault_clause

    lines = ["## Changes since previous run", ""]
    if delta is None:
        lines.append(
            "First published baseline unavailable — no previous snapshot to compare."
        )
        return lines

    if previous_run_id:
        lines.append(f"*Compared to `{previous_run_id}`.*")
        lines.append("")

    for note in delta.get("notes") or []:
        lines.append(f"*Note: {note}*")
        lines.append("")

    failed = list(delta.get("newly_failed_services") or [])
    history = delta.get("item_history")
    recovered_devs = list(delta.get("recovered_devices") or [])
    missing = list(delta.get("newly_missing_evidence") or [])
    persistent = list(delta.get("persistent") or [])
    # Recovered problems that are not already covered by recovered_devices.
    recovered_probs = [
        i
        for i in (delta.get("recovered") or [])
        if isinstance(i, dict)
        and str(i.get("code") or "") != "device_live_unreachable"
    ]

    if not delta_has_operational_changes(delta) and not recovered_probs:
        lines.append(
            "No material changes — no newly detected service faults, recovered devices, "
            "newly missing evidence, or persistent open problems versus the "
            "previous run."
        )
        return lines

    lines.append("**Newly detected service faults:**")
    if not failed:
        lines.append("- None")
    else:
        for item in failed:
            name = item.get("service") or "?"
            st = item.get("status") or "?"
            detail = item.get("detail") or ""
            stype = item.get("service_type")
            label = f"`{stype}/{name}`" if stype else f"`{name}`"
            bit = f"- {label} — {st}"
            if detail:
                bit += f" ({detail})"
            lines.append(bit)
    lines.append("")

    if failed:
        lines.append("A newly detected fault does not prove a regression: prior "
                     "readiness may have used different checks. Failure onset is unverified.")
        lines.append("")
    still_open = list(delta.get("open_faults") or [])
    if still_open:
        from diagnostic_mas.open_faults import format_open_faults

        lines.append("**Service faults still open:**")
        lines.extend(format_open_faults(still_open, limit=persistent_limit))
        lines.append("")
    moved = list(delta.get("status_changed_on_unchanged_checks") or [])
    if moved:
        lines.append("**Status changed while the fixed checks read the same:**")
        lines.extend(f"- {_status_change_line(row)}" for row in moved[:persistent_limit])
        extra = len(moved) - persistent_limit
        if extra > 0:
            lines.append(f"- …and {extra} more")
        lines.append("")
    lines.extend(format_last_known_faults(list(delta.get("last_known_service_faults") or [])))
    cleared = list(delta.get("service_faults_cleared") or [])
    if cleared:
        lines.append("**Service faults no longer observed:**")
        lines.extend(f"- {_cleared_line(row)}" for row in cleared[:persistent_limit])
        extra = len(cleared) - persistent_limit
        if extra > 0:
            lines.append(f"- …and {extra} more")
        lines.append("")
    lost = list(delta.get("routing_sessions_lost") or [])
    if lost:
        lines.append("**Routing sessions up in the previous run, not up now:**")
        for row in lost[:persistent_limit]:
            lines.append(f"- {_routing_session_line(row)}")
        extra = len(lost) - persistent_limit
        if extra > 0:
            lines.append(f"- …and {extra} more")
        lines.append("")
    regained = list(delta.get("routing_sessions_regained") or [])
    if regained:
        lines.append("**Routing sessions not up in the previous run, up now:**")
        for row in regained[:persistent_limit]:
            lines.append(f"- {_routing_session_line(row)}")
        extra = len(regained) - persistent_limit
        if extra > 0:
            lines.append(f"- …and {extra} more")
        lines.append("")
    changed = list(delta.get("adjacencies_changed_between_scans") or [])
    if changed:
        lines.append("**IS-IS adjacencies up in both scans, last changed after the previous scan began:**")
        lines.extend(f"- {_adjacency_change_line(row, history)}" for row in changed[:persistent_limit])
        extra = len(changed) - persistent_limit
        if extra > 0:
            lines.append(f"- …and {extra} more")
        lines.append("")
    gaining = list(delta.get("links_gaining_errors") or [])
    if gaining:
        from diagnostic_mas.port_counters import gain_lines

        # A lead on a link that still works; no service status rests on it. One line per link.
        links = gain_lines(gaining, history)
        lines.append("**Links gaining errors while up:**")
        lines.extend(f"- {line}" for line in links[:persistent_limit])
        extra = len(links) - persistent_limit
        if extra > 0:
            lines.append(f"- …and {extra} more")
        lines.append("")
    fewer = list(delta.get("sessions_receiving_fewer_prefixes") or [])
    if fewer:
        # A lead on a session that still works, like a link gaining errors.
        lines.append("**BGP sessions still Established but receiving far fewer prefixes:**")
        lines.extend(f"- {prefix_drop_line(row)}" for row in fewer[:persistent_limit])
        extra = len(fewer) - persistent_limit
        if extra > 0:
            lines.append(f"- …and {extra} more")
        lines.append("")
    interface_lines = _interface_lines(list(delta.get("interfaces_lost") or []))
    if interface_lines:
        lines.append("**Interfaces up in the previous run, down now:**")
        lines.extend(f"- {line}" for line in interface_lines[:persistent_limit])
        extra = len(interface_lines) - persistent_limit
        if extra > 0:
            lines.append(f"- …and {extra} more devices")
        lines.append("")
    lines.append("**Recovered devices:**")
    if not recovered_devs and not recovered_probs:
        lines.append("- None")
    else:
        for device in recovered_devs:
            lines.append(
                f"- `{device}` — a current-run live query succeeded "
                "(prior `device_live_unreachable` cleared)"
            )
        for issue in recovered_probs[:persistent_limit]:
            lines.append(f"- {_issue_line(issue)}")
        extra = len(recovered_probs) - persistent_limit
        if extra > 0:
            lines.append(f"- …and {extra} more recovered problem(s)")
    lines.append("")

    lines.append("**Newly missing evidence:**")
    if not missing:
        lines.append("- None")
    else:
        for item in missing:
            subj = item.get("subject") or "?"
            detail = item.get("detail") or item.get("kind") or "gap"
            kind = item.get("kind") or "gap"
            lines.append(f"- `{subj}` — {detail} ({kind})")
    lines.append("")

    gone = list(delta.get("services_no_longer_present") or [])
    if gone:
        lines.append("**Services no longer present since the previous run:**")
        for row in gone[:persistent_limit]:
            lines.append(f"- `{row['service_type']}/{row['service']}`")
        extra = len(gone) - persistent_limit
        if extra > 0:
            lines.append(f"- …and {extra} more")
        lines.append("")

    lines.append("**Persistent problems:**")
    if not persistent:
        lines.append("- None")
    else:
        for issue in persistent[:persistent_limit]:
            age = routing_fault_clause(issue, history)
            lines.append(f"- {_issue_line(issue)}" + (f"; {age}" if age else ""))
        extra = len(persistent) - persistent_limit
        if extra > 0:
            lines.append(
                f"- …and {extra} more unchanged open problem(s) "
                "(see Devices / Services; not re-listed here)"
            )
        elif len(persistent) > 1:
            lines.append(
                "*Unchanged open items — details under Devices / Services; "
                "not re-summarized as new findings.*"
            )
    return lines


def compact_delta_for_llm(delta: CaseDelta | None) -> dict[str, Any] | None:
    """Small delta payload for the summary LLM (no full issue dumps)."""
    from diagnostic_mas.item_history import routing_fault_clause

    if delta is None:
        return None
    history = delta.get("item_history")

    def _trim_issues(items: list[Any], limit: int = 12) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for issue in items[:limit]:
            if not isinstance(issue, dict):
                continue
            out.append(
                {
                    "code": issue.get("code"),
                    "layer": issue.get("layer"),
                    "edge_id": issue.get("edge_id"),
                    "devices": issue.get("devices"),
                    "message": str(issue.get("message") or "")[:160],
                    "status": issue.get("status"),
                    **({"recurrence": age} if (age := routing_fault_clause(issue, history)) else {}),
                }
            )
        return out

    gone = [row["service"] for row in delta.get("services_no_longer_present") or []]
    lost = [_routing_session_line(row).replace("`", "")
            for row in delta.get("routing_sessions_lost") or []]
    regained = [_routing_session_line(row).replace("`", "")
                for row in delta.get("routing_sessions_regained") or []]
    changed = [_adjacency_change_line(row, history).replace("`", "")
               for row in delta.get("adjacencies_changed_between_scans") or []]
    interfaces = list(delta.get("interfaces_lost") or [])
    cleared = [f"{row['service']} (was {row['was']})" if row.get("was") else row["service"]
               for row in delta.get("service_faults_cleared") or []]
    moved = [_status_change_line(row).replace("`", "")
             for row in delta.get("status_changed_on_unchanged_checks") or []]
    new = list(delta.get("new_problems") or [])
    from diagnostic_mas.open_faults import format_open_faults

    open_faults = list(delta.get("open_faults") or [])
    fewer = [prefix_drop_line(row).replace("`", "") for row in delta.get("sessions_receiving_fewer_prefixes") or []]
    links: list[str] = []
    if delta.get("links_gaining_errors"):
        from diagnostic_mas.port_counters import gain_lines

        links = [line.replace("`", "") for line in gain_lines(list(delta["links_gaining_errors"]), history)]
    return {
        **({"service_faults_cleared": {
            "count": len(cleared),
            "services": cleared[:20],
            "note": ("Down or degraded in an earlier run; they pass this run's checks. "
                     "PE-side readiness only; customer delivery was not tested."),
        }} if cleared else {}),
        **({"status_changed_on_unchanged_checks": {
            "count": len(moved),
            "services": moved[:20],
            "note": ("Final status differs from the previous run although each service's fixed checks "
                     "read the same in both scans: the change comes from an LLM investigation's verdict, "
                     "or from one that could not finish. A status change, but neither a new fault nor a "
                     "recovery on its own."),
        }} if moved else {}),
        **({"interfaces_lost": {
            "count": len(interfaces),
            "by_device": [line.replace("`", "") for line in _interface_lines(interfaces)][:12],
            "note": ("Up in the previous run and recorded down in this one. "
                     "admin-down means shut down by configuration. \"its service was deleted\" "
                     "means every service the previous run had on it is gone from this run, "
                     "deleted by its owner; the change most likely follows the deletion and is "
                     "not evidence of a fault on its own. \"already Down in the previous run\" "
                     "means that run read the port up, then found its services Down: the loss "
                     "was already reported then and is not new."),
        }} if interfaces else {}),
        **({"routing_sessions_lost": {
            "count": len(lost),
            "sessions": lost[:20],
            "note": ("Up in the previous run. Either the devices now report the "
                     "session down, or it is absent although both devices answered."),
        }} if lost else {}),
        **({"routing_sessions_regained": {
            "count": len(regained),
            "sessions": regained[:20],
            "note": ("Not up in the previous run; this run reads them up. A line naming one end "
                     "is that end's view only: the other end was not read."),
        }} if regained else {}),
        **({"adjacencies_changed_between_scans": {
            "count": len(changed),
            "adjacencies": changed[:20],
            "note": ("Up in both scans; the router's last state change came after the previous scan "
                     "began, so it went down and came back since, unless the change fell in the "
                     "minutes before that scan read it. Up now; no service status rests on it."),
        }} if changed else {}),
        **({"links_gaining_errors": {
            "count": len(links),
            "links": links[:10],
            "note": ("Up now; gained errors or changed state since the previous scan. A lead on a "
                     "working link, not a fault: no service status rests on it."),
        }} if links else {}),
        **({"sessions_receiving_fewer_prefixes": {
            "count": len(fewer),
            "sessions": fewer[:10],
            "note": ("Established in both scans; this end received at least 1,000 prefixes in the previous "
                     "scan and fewer than half now. A lead on a working session, not a fault: no service "
                     "status rests on it."),
        }} if fewer else {}),
        **({"services_no_longer_present": {
            "count": len(gone),
            "services": gone[:20],
            "note": ("Not present in NSO any more since the previous run. "
                     "Routine; not a coverage gap, a fault or a recovery."),
        }} if gone else {}),
        "newly_failed_services": list(delta.get("newly_failed_services") or [])[:20],
        "recovered_devices": list(delta.get("recovered_devices") or [])[:20],
        "newly_missing_evidence": list(delta.get("newly_missing_evidence") or [])[
            :20
        ],
        "last_known_service_faults": list(delta.get("last_known_service_faults") or [])[:20],
        **({"open_faults": {
            "count": len(open_faults),
            "services": sum(len(group["services"]) for group in open_faults),
            "lines": [line.removeprefix("- ").replace("`", "")
                      for line in format_open_faults(open_faults, limit=20)],
            "note": ("The faults still open, one line per port, cut-off device or shared reason that "
                     "explains them, else per service: last_known_service_faults grouped as the "
                     "report lists them. count is the number of lines."),
        }} if open_faults else {}),
        "regression_policy": "No proven regression from status changes alone; comparable checks are required. Failure onset is unverified.",
        "persistent_count": len(delta.get("persistent") or []),
        "persistent_sample": _trim_issues(list(delta.get("persistent") or [])),
        "recovered_problem_count": len(delta.get("recovered") or []),
        **({"new_problems": {
            "count": len(new),
            "sample": _trim_issues(new),
            "note": ("Recorded this run, not by the previous run under the same code and subject. "
                     "That alone does not prove a new failure: an existing condition can be "
                     "recorded under a new code; read each message."),
        }} if new else {}),
    }
