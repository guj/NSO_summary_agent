"""Investigate each down port and each cut-off device once, before the services.

A port qualifies when a service whose fixed check is Down attaches to a link
the interface collection recorded as down, and every faulted check of that
service is on such a link. The port's finding then explains those services:
they keep the Down from their own fixed check and are not investigated
individually. A port investigation that does not conclude explains nothing,
and neither does one whose own reading no longer shows the link down: the
state may have changed since it was collected.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from diagnostic_mas.case import CaseFile, DrillSession, add_evidence, open_issue, set_issue_status
from nso_facts.mcp_client import successful_live_devices
from nso_facts.topology.interfaces import interfaces_match

SOURCE = "port_investigation"
COVERAGE = "port_investigated"
DEVICE_SOURCE = "device_investigation"
DEVICE_COVERAGE = "device_investigated"
SOURCES = frozenset({SOURCE, DEVICE_SOURCE})
_PROMPT = Path(__file__).resolve().parent / "prompts" / "port_agent.txt"
_DEVICE_PROMPT = Path(__file__).resolve().parent / "prompts" / "device_agent.txt"


def _link_of(interface: str) -> str:
    return re.sub(r"\.\d+$", "", interface)


def port_of(interface: str) -> str:
    """Physical port of an interface: sub-interface and breakout lane removed."""
    link = _link_of(interface)
    lane = re.match(r"^(.*\d+/\d+/\d+/\d+)/\d+$", link)
    return lane.group(1) if lane else link


def _service_spines(case: CaseFile):
    for ev in case.evidence:
        extra = (ev.get("payload") or {}).get("extra")
        if ev.get("kind") == "spine" and ev.get("role") == "service" and isinstance(extra, dict):
            yield ev, extra


def _down_links(case: CaseFile) -> dict[str, list[str]]:
    """Device -> links the interface collection recorded as down, not shut."""
    out: dict[str, list[str]] = {}
    for _ev, extra in _service_spines(case):
        for edge in extra.get("physical_operational_edges") or []:
            device, _, name = str(edge.get("id") or "").removeprefix("if:").partition(":")
            if (edge.get("state") or {}).get("oper") == "down" and name == _link_of(name):
                out.setdefault(device, []).append(name)
    return out


def _faulted_links(record: dict[str, Any], down: dict[str, list[str]]) -> set[tuple[str, str]]:
    """(device, link) behind each faulted check; empty unless every fault is on a down link."""
    links: set[tuple[str, str]] = set()
    basic = record.get("basic_checks") or {}
    if basic.get("status") != "down":
        return links
    for check in basic.get("checks") or []:
        if check.get("status") != "fault":
            continue
        device = str(check.get("device") or "")
        named = _link_of(str(check.get("observation") or "").split(" ", 1)[0])
        link = next((name for name in down.get(device, []) if named and interfaces_match(named, name)), None)
        if link is None:
            return set()
        links.add((device, link))
    return links


def _records(case: CaseFile):
    for ev, extra in _service_spines(case):
        for record in (extra.get("services") or {}).values():
            if isinstance(record, dict) and record.get("name"):
                yield ev, record


def port_targets(case: CaseFile) -> list[dict[str, Any]]:
    """Down ports to investigate, those explaining the most services first."""
    down = _down_links(case)
    ports: dict[tuple[str, str], dict[str, list[str]]] = {}
    for _ev, record in _records(case):
        for device, link in _faulted_links(record, down):
            ports.setdefault((device, port_of(link)), {}).setdefault(link, []).append(str(record["name"]))
    targets = [
        {"device": device, "port": port,
         "links": {link: sorted(names) for link, names in sorted(links.items())}}
        for (device, port), links in ports.items()
    ]
    targets.sort(key=lambda t: (-sum(map(len, t["links"].values())), t["device"], t["port"]))
    return targets


def _system_prompt() -> str:
    if _PROMPT.is_file():
        return _PROMPT.read_text(encoding="utf-8")
    return "Investigate why this port is down via mcp_call, then call conclude_investigation."


def _issue_message(target: dict[str, Any]) -> str:
    links = "; ".join(
        f"link {link} recorded down, {len(names)} service{'s' if len(names) != 1 else ''} attached"
        for link, names in target["links"].items())
    return f"{target['device']} port {target['port']} is down: {links}"


def port_findings(case: CaseFile) -> list[dict[str, Any]]:
    """Recorded port investigations, in the order they ran."""
    return [ev["payload"] for ev in case.evidence
            if ev.get("kind") == SOURCE and isinstance(ev.get("payload"), dict)]


def _may_investigate(case: CaseFile) -> bool:
    from agent.llm_budget import case_llm_halted

    budget = case.budget
    return (budget.port_investigations_used < budget.max_port_investigations
            and budget.max_tools_per_drill > 0 and not case_llm_halted(case))


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _readings(case: CaseFile, mark: int) -> list[tuple[str, str]]:
    """(command, device text) for each answer an investigation recorded since ``mark``."""
    out: list[tuple[str, str]] = []
    for ev in case.evidence[mark:]:
        payload = ev.get("payload") or {}
        if ev.get("kind") == "drill":
            command = str((payload.get("args") or {}).get("input_command") or "")
            out.extend((command, text) for text in _strings(payload.get("result")))
    return out


def _link_states(links: Any, readings: list[tuple[str, str]]) -> dict[str, str]:
    """Each link as the investigation itself read it: down, up, or unread."""
    from diagnostic_mas.operational_checks.probe import interface_state

    states = {str(link): "unread" for link in links}
    for _command, text in readings:
        for link in states:
            seen = interface_state(text, link)
            if seen != "unknown":
                states[link] = "up" if seen == "pass" else "down"
    return states


def _sessions_state(device: str, readings: list[tuple[str, str]]) -> str:
    """The device's BGP sessions as the investigation itself read them."""
    from nso_facts.topology.routing import parse_bgp_summary_text

    states = [
        str(row.state).lower()
        for command, text in readings if "bgp" in command and "summary" in command
        for row in parse_bgp_summary_text(device, text)
    ]
    if not states:
        return "unread"
    return "up" if any(state.startswith("estab") for state in states) else "down"


async def _investigate(
    client: Any, settings: Any, case: CaseFile, target: dict[str, Any], *,
    kind: str, code: str, layer: str, edge_id: str, message: str, prompt: str,
    device_names: set[str], openai_client: Any | None, tool_loop: Any | None,
    confirm: Any,
) -> dict[str, Any]:
    """Run one investigation, record it as evidence and return what was recorded.

    ``confirm`` turns the investigation's own readings into the fields that
    say whether the fault it was sent to explain was still there.
    """
    if tool_loop is None:
        from diagnostic_mas.drill import llm_drill_tool_loop as tool_loop
    budget = case.budget
    budget.port_investigations_used += 1
    issue_id = open_issue(
        case, code=code, severity="high", layer=layer, edge_id=edge_id,
        devices=[target["device"]], message=message, evidence_ids=[])
    issue = next(i for i in case.issues if i.get("id") == issue_id)
    session = DrillSession(max_tools=budget.max_tools_per_drill, issue_id=issue_id, issue_edge_id=edge_id)
    mark = len(case.evidence)
    _used, done = await tool_loop(
        client, settings, case, device_names=device_names, focus_issue=issue, session=session,
        openai_client=openai_client, system_prompt=prompt, account="port")
    found = next((ev for ev in case.evidence[mark:] if ev.get("kind") == "drill_finding"), None)
    payload: dict[str, Any] = {**target, "concluded": bool(done and found), "tools_used": session.tools_used,
                               **confirm(_readings(case, mark))}
    if payload["concluded"]:
        finding = found["payload"]
        payload.update({key: finding.get(key) for key in ("observed", "cause", "fix_suggestion", "confidence")},
                       finding_evidence_id=found.get("id"))
        set_issue_status(case, issue_id, "explained")
    add_evidence(case, {"kind": kind, "role": "port", "layer": layer, "payload": payload})
    return payload


async def run_port_investigations(
    client: Any,
    settings: Any,
    case: CaseFile,
    *,
    device_names: set[str],
    openai_client: Any | None = None,
    tool_loop: Any | None = None,
) -> set[str]:
    """Investigate qualifying ports within the ceiling; return the services they explain."""
    from diagnostic_mas.drill import _quarantined_device_map

    unavailable = _quarantined_device_map()
    concluded: dict[tuple[str, str], dict[str, Any]] = {}
    for target in port_targets(case):
        if not _may_investigate(case):
            break
        if target["device"] in unavailable:
            continue
        payload = await _investigate(
            client, settings, case, target, kind=SOURCE, code="port_down", layer="physical",
            edge_id=f"if:{target['device']}:{target['port']}", message=_issue_message(target),
            prompt=_system_prompt(), device_names=device_names, openai_client=openai_client, tool_loop=tool_loop,
            confirm=lambda readings, links=target["links"]: {"link_states": _link_states(links, readings)})
        if payload["concluded"]:
            concluded[target["device"], target["port"]] = payload
    return _explain_services(case, concluded)


def _explain_services(case: CaseFile, concluded: dict[tuple[str, str], dict[str, Any]]) -> set[str]:
    """Record the port findings against the services they fully explain."""
    from diagnostic_mas.dataplane_verify import _record_dataplane_finding, _refresh_spine_counts

    down = _down_links(case)
    explained: set[str] = set()
    for ev, record in list(_records(case)):
        links = sorted(_faulted_links(record, down))
        ports = {(device, port_of(link)) for device, link in links}
        if not links or not ports <= concluded.keys():
            continue
        if any(concluded[device, port_of(link)]["link_states"].get(link) != "down" for device, link in links):
            continue
        findings = [concluded[key] for key in sorted(ports)]
        where = "; ".join(f"{link} on {device}" for device, link in links)
        causes = " ".join(
            f"Port {f['port']} was investigated once: {brief(f.get('cause'), 240)}" for f in findings)
        _record_dataplane_finding(case, record, {
            "dataplane_status": "down",
            "cause": f"Attached link {where} is down. {causes}",
            "observed": (
                f"Fixed check (this run) faulted on {where}; the interface collection recorded it down. "
                "This service was not investigated individually; the readings are in the port finding under "
                + ", ".join(sorted({f"{f['device']} {f['port']}" for f in findings})) + "."),
            "fix_suggestion": next((f["fix_suggestion"] for f in findings if f.get("fix_suggestion")), None),
            "confidence": min((str(f.get("confidence") or "medium") for f in findings),
                              key=("low", "medium", "high").index),
        }, source=SOURCE, evidence_ids=[f["finding_evidence_id"] for f in findings if f.get("finding_evidence_id")])
        case.service_coverage[str(record["name"])] = COVERAGE
        _refresh_spine_counts(ev)
        explained.add(str(record["name"]))
    return explained


def brief(text: Any, limit: int) -> str:
    """Text cut to ``limit`` at a sentence end where one exists, else between words."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    head = text[:limit]
    end = head.rfind(". ")
    return head[:end + 1] if end > limit // 4 else head.rsplit(" ", 1)[0] + "…"


def cut_off_devices(case: CaseFile, answered: set[str]) -> list[dict[str, Any]]:
    """Devices that answered this scan's routing queries and have nothing up.

    Cut off means: at least one BGP session to an inventory device is reported
    down, none is up, and no IS-IS adjacency is up. A device that did not
    answer, or whose routing query failed, is never called cut off.
    """
    known = set(case.device_names or [])
    failed = {
        str(issue.get("message") or "").split(":", 1)[0].strip()
        for issue in case.issues
        if issue.get("code") == "collection_error" and issue.get("layer") in {"underlay", "routing"}
    }
    connected: set[str] = set()
    down: dict[str, list[str]] = {}
    for ev in case.evidence:
        if ev.get("kind") != "spine" or ev.get("role") not in {"bgp", "isis"}:
            continue
        for edge in (ev.get("payload") or {}).get("operational_edges") or []:
            status = (edge.get("state") or {}).get("status")
            for device in (part for part in str(edge.get("id") or "").split(":") if part in known):
                if status in {"up", "degraded"}:
                    connected.add(device)
                elif status == "down" and ev.get("role") == "bgp":
                    down.setdefault(device, []).append(str(edge["id"]))
    return [
        {"device": device, "sessions_down": sorted(sessions)}
        for device, sessions in sorted(down.items())
        if device not in connected and device in answered and device not in failed
    ]


def _dependents(case: CaseFile, device: str):
    """Services on the device that do not pass and whose own config names another device."""
    diagnosed = {
        str((dx.get("subject") or {}).get("name") or "")
        for dx in case.diagnoses if dx.get("kind") == "dataplane"
    }
    for ev, record in _records(case):
        devices = {str(d) for d in record.get("devices") or []}
        status = (record.get("basic_checks") or {}).get("status")
        if (device in devices and len(devices) > 1 and status in {"down", "unknown"}
                and str(record["name"]) not in diagnosed):
            yield ev, record


def _device_prompt() -> str:
    if _DEVICE_PROMPT.is_file():
        return _DEVICE_PROMPT.read_text(encoding="utf-8")
    return "Investigate why this device has no adjacency or session up, then call conclude_investigation."


async def run_device_investigations(
    client: Any,
    settings: Any,
    case: CaseFile,
    *,
    device_names: set[str],
    answered: set[str] | None = None,
    openai_client: Any | None = None,
    tool_loop: Any | None = None,
) -> set[str]:
    """Investigate each cut-off device once; return the services it explains.

    A service is explained when it does not pass its fixed check and its own
    config names another device, which a cut-off device cannot reach. Services
    whose config is silent about other devices are left as they are.
    """
    from diagnostic_mas.dataplane_verify import _record_dataplane_finding, _refresh_spine_counts

    if answered is None:
        answered = set(successful_live_devices())
    explained: set[str] = set()
    for target in cut_off_devices(case, answered):
        device = target["device"]
        dependents = list(_dependents(case, device))
        if not dependents:
            continue
        if not _may_investigate(case):
            break
        n = len(target["sessions_down"])
        target = {**target, "services": sorted(str(r["name"]) for _ev, r in dependents)}
        payload = await _investigate(
            client, settings, case, target, kind=DEVICE_SOURCE, code="device_cut_off", layer="routing",
            edge_id=f"device:{device}",
            message=(f"{device} answered this scan's queries but has no IS-IS adjacency up and "
                     f"{n} BGP session{'s' if n != 1 else ''} down, none up; "
                     f"{len(dependents)} of its services use another device"),
            prompt=_device_prompt(), device_names=device_names, openai_client=openai_client, tool_loop=tool_loop,
            confirm=lambda readings, device=device: {"sessions_now": _sessions_state(device, readings)})
        if not payload["concluded"] or payload["sessions_now"] != "down":
            continue
        for ev, record in dependents:
            others = ", ".join(sorted({str(d) for d in record.get("devices") or []} - {device}))
            _record_dataplane_finding(case, record, {
                "dataplane_status": "down",
                "cause": (f"{device} has no IS-IS adjacency and no BGP session up, and this service also "
                          f"uses {others}. The device was investigated once: {brief(payload.get('cause'), 240)}"),
                "observed": (
                    f"This scan: {device} answered the routing queries, reported {n} BGP "
                    f"session{'s' if n != 1 else ''} down and none up, and no adjacency up. The fixed check "
                    f"for this service was {(record.get('basic_checks') or {}).get('status')}. This service "
                    f"was not investigated individually; the readings are in the cut-off finding under {device}."),
                "fix_suggestion": payload.get("fix_suggestion"),
                "confidence": str(payload.get("confidence") or "medium"),
            }, source=DEVICE_SOURCE,
                evidence_ids=[payload["finding_evidence_id"]] if payload.get("finding_evidence_id") else [])
            case.service_coverage[str(record["name"])] = DEVICE_COVERAGE
            _refresh_spine_counts(ev)
            explained.add(str(record["name"]))
    return explained


def device_findings(case: CaseFile) -> list[dict[str, Any]]:
    """Recorded cut-off device investigations, in the order they ran."""
    return [ev["payload"] for ev in case.evidence
            if ev.get("kind") == DEVICE_SOURCE and isinstance(ev.get("payload"), dict)]
