"""Bounded dataplane scheduling with isolated cases and device reservations.

Only the experimental checkout uses this module. No service-specific diagnoses.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from copy import copy, deepcopy
from dataclasses import replace
from urllib.parse import unquote
import re

from agent.llm_budget import ProviderBudgetExceeded, case_llm_halted, mark_case_llm_halt
from diagnostic_mas.case import DrillSession, add_evidence

_parent_case: ContextVar[object | None] = ContextVar('dataplane_parent_case', default=None)


def parent_halted():
    parent = _parent_case.get()
    return parent is not None and case_llm_halted(parent)


async def llm_request(investigation, create, **kwargs):
    # Keep the original sequential path unchanged; only workers offload HTTP.
    if _parent_case.get() is None:
        return investigation.llm(create, **kwargs)
    if parent_halted():
        raise ProviderBudgetExceeded('Provider budget halted this run')
    return await asyncio.to_thread(investigation.llm, create, **kwargs)


def endpoints(record):
    values = record.get('devices')
    if not isinstance(values, (list, tuple, set)) or not values:
        return None
    if any(not isinstance(d, str) or not d.strip() for d in values):
        return None
    return {d.strip() for d in values}


class Reservations:
    def __init__(self):
        self.active = {}  # worker -> set of devices, or None for exclusive

    def available(self, devices):
        if not self.active:
            return True
        return devices is not None and all(
            held is not None and not devices.intersection(held)
            for held in self.active.values()
        )

    def reserve_extra(self, worker, devices):
        held = self.active[worker]
        if held is None:
            return True
        others = [v for k, v in self.active.items() if k != worker]
        if devices is None:
            if others:
                return False
            self.active[worker] = None
            return True
        if any(v is None or devices.intersection(v) for v in others):
            return False
        held.update(devices)
        return True


# Explicit CDB readers only; sync/compare and operational tools stay guarded.
CDB_READ_TOOLS = frozenset({
    'get_services', 'get_service_types', 'list_devices', 'get_device_config',
    'get_device_groups', 'get_device_ned_ids',
})


def cdb_path(path, record):
    """Recognize stored configuration, never a device's live-status subtree.

    Service roots come from the selected record, not a service-type allowlist.
    Unknown roots and broad device trees retain conservative reservations.
    """
    parts = unquote(str(path)).strip('/').split('/')
    names = [part.split(':')[-1] for part in parts]
    if any(p in {'.', '..', '*'} for p in names):
        return False
    if (len(names) >= 3 and names[0] == 'devices'
            and names[1].startswith('device=') and names[2] == 'config'):
        return True
    # Discovering the NSO services container reads CDB, not device live-status.
    # Limit this exception to the exact known root; do not exempt arbitrary
    # namespaces or unknown descendants merely because they contain 'services'.
    if len(parts) == 1 and parts[0] in {'tailf-ncs:services', 'ncs:services'}:
        return True
    if names and names[0] == 'services':
        names = names[1:]
    service_type = str(record.get('service_type') or '').split(':')[-1]
    return bool(service_type and names and
                names[0].split('=', 1)[0] == service_type)


class ReservedClient:
    """Guard normalized wire calls, including any extra device requested by LLM."""
    def __init__(self, client, reservations, worker, record):
        self.client, self.reservations, self.worker, self.record = client, reservations, worker, record

    def __getattr__(self, name):
        return getattr(self.client, name)

    def reserve_call(self, name, arguments):
        if parent_halted():
            raise ProviderBudgetExceeded('Provider budget halted this run')
        params = arguments.get('params', arguments)
        device = params.get('device_name') or params.get('device')
        devices = {device} if isinstance(device, str) and device else None
        # These inventory reads do not connect to devices.
        if name in CDB_READ_TOOLS:
            devices = set()
        elif name == 'explore_nso_path':
            path = str(params.get('path') or '')
            match = re.search(r'(?:^|/)device=([^/]+)', path)
            devices = (set() if cdb_path(path, self.record) else
                       {unquote(match.group(1))} if match else None)
        elif name in {'check_service_sync', 'compare_service_config'}:
            same = (params.get('service_name') == self.record.get('name') and
                    params.get('service_type') == self.record.get('service_type'))
            devices = endpoints(self.record) if same else None
        if not self.reservations.reserve_extra(self.worker, devices):
            raise RuntimeError(
                'Concurrency reservation: requested device/scope is in use by another dig. '
                'No MCP request was sent. This is not network-fault evidence. '
                'Continue with available evidence; if needed conclude unknown and identify this deferred check.'
            )
    async def call_tool(self, name, arguments):
        self.reserve_call(name, arguments)
        return await self.client.call_tool(name, arguments)


def isolate_case(case):
    child = copy(case)
    child.budget = replace(case.budget, dataplane_tools_used=0)
    child.evidence = list(case.evidence)
    child.diagnoses = list(case.diagnoses)
    child.issues = deepcopy(case.issues)
    child.service_coverage = dict(case.service_coverage)
    return child, len(child.evidence), len(child.diagnoses)


def merge_case(parent, child, evidence_start, diagnosis_start, name):
    # Single event-loop operation (no await): unique IDs and local citations.
    mapping = {}
    for row in child.evidence[evidence_start:]:
        row = deepcopy(row)
        old = row.pop('id', None)
        mapping[old] = add_evidence(parent, row)
    for row in child.diagnoses[diagnosis_start:]:
        row = deepcopy(row)
        parent._diagnosis_seq += 1
        row['id'] = f'dx_{parent._diagnosis_seq}'
        row['evidence_ids'] = [mapping.get(e, e) for e in row.get('evidence_ids', [])]
        parent.diagnoses.append(row)
    updates = {i['id']: i for i in child.issues if i.get('edge_id') == name and i.get('layer') == 'services'}
    for issue in parent.issues:
        if issue.get('id') in updates:
            issue.update(updates[issue['id']])
    if name in child.service_coverage:
        parent.service_coverage[name] = child.service_coverage[name]
    parent.budget.dataplane_tools_used += child.budget.dataplane_tools_used
    if child.llm_halt_reason:
        mark_case_llm_halt(parent, child.llm_halt_reason)


async def run_concurrent(candidates, *, workers, client, settings, case,
                         device_names, tools_cap, openai_client, verify_one, log):
    reservations = Reservations()
    pending = list(enumerate(candidates))
    active = {}
    touched = set()

    async def run_one(index, ev, record):
        child, ev_start, dx_start = isolate_case(case)
        local_record = deepcopy(record)
        session = DrillSession(max_tools=tools_cap, issue_edge_id=str(record.get('name') or ''))
        token = _parent_case.set(case)
        log(f'worker={index + 1} start service={record.get("name")} devices={record.get("devices")}')
        try:
            ok = await verify_one(
                ReservedClient(client, reservations, index, record), settings, child,
                record=local_record, device_names=device_names, session=session,
                openai_client=openai_client,
            )
            if ok:
                touched.add(id(ev))
        except ProviderBudgetExceeded as exc:
            mark_case_llm_halt(case, str(exc))
        finally:
            merge_case(case, child, ev_start, dx_start, record.get('name'))
            record.update(local_record)
            reservations.active.pop(index, None)
            _parent_case.reset(token)
            log(f'worker={index + 1} finished service={record.get("name")} tools={session.tools_used}')

    try:
        while pending or active:
            while len(active) < workers and pending and not case_llm_halted(case):
                chosen = next((j for j, (_, (_, rec)) in enumerate(pending)
                               if reservations.available(endpoints(rec))), None)
                if chosen is None:
                    break
                index, (ev, rec) = pending.pop(chosen)
                reservations.active[index] = endpoints(rec)
                task = asyncio.create_task(run_one(index, ev, rec))
                active[task] = index
            if not active:
                break
            done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            errors = []
            for task in done:
                active.pop(task)
                try:
                    task.result()
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                raise errors[0]
    finally:
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
    if case_llm_halted(case):
        for _, (_, record) in pending:
            case.service_coverage[str(record.get('name') or '')] = 'llm_budget_exceeded'
    return touched
