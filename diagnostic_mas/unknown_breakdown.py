"""Report verification gaps without turning missing evidence into network faults."""
import re

CATEGORIES = {
    'access_failure': 'Access failure',
    'identity_missing': 'Service identity missing',
    'forwarding_incomplete': 'Forwarding evidence incomplete',
    'gate_contradiction': 'Gate contradiction',
}


def category_for(dx):
    gap = dx.get('verification_gap') or {}
    # Only a separately recorded review can confirm a validator contradiction.
    review = dx.get('gate_review') or {}
    if (review.get('outcome') == 'contradiction_confirmed' and
            review.get('reason') and review.get('evidence_ids')):
        return 'gate_contradiction'
    blocker = gap.get('blocker')
    if blocker == 'gate_rejected' or dx.get('source') == 'gate':
        return 'forwarding_incomplete'
    if blocker == 'endpoint_access_unavailable':
        return 'access_failure'
    if blocker == 'identity_unverified':
        return 'identity_missing'
    declared = gap.get('category')
    if declared in {'access_failure', 'identity_missing', 'forwarding_incomplete'}:
        return declared
    return 'forwarding_incomplete'


def pending_gate_review(dx):
    return category_for(dx) != 'gate_contradiction' and (
        (dx.get('verification_gap') or {}).get('blocker') == 'gate_rejected'
        or dx.get('source') == 'gate')


def _refusal(dx):
    """(verdict the investigation reached, the evidence check's reason), read from the recorded cause.

    The cause reads "[gate] <reason> (LLM claimed <verdict>: <its own text>)"; some reasons
    begin "<type> <verdict> refused: ".
    """
    cause = str(dx.get('cause') or '')
    claimed = re.search(r'\(LLM claimed (up|down|degraded)\b', cause)
    head = cause.split(' (LLM claimed', 1)[0]
    refused = re.search(r'\b(up|down|degraded)\s+refused:\s*(.+)', head)
    if refused:
        text, verdict = refused[2], refused[1]
    elif head.startswith('[gate]'):
        text, verdict = head[len('[gate]'):], claimed[1] if claimed else None
    else:
        return (claimed[1] if claimed else None), None
    # The reason's first sentence is for the reader; what follows it is guidance for the model.
    reason = re.split(r'\.\s', text, maxsplit=1)[0].strip().rstrip('.')
    return verdict, reason or None


def refused_verdict(dx):
    """The next check for a service whose investigation's verdict the evidence check did not accept."""
    claimed, reason = _refusal(dx)
    said = f'reported {claimed}' if claimed else 'reached a verdict'
    because = f': {reason}' if reason else ''
    return (f'The investigation {said}, but that was not accepted{because}. '
            'It stays Unknown, which is not a fault; the next scan checks it again.')


def refused_followup(name, dx):
    claimed, reason = _refusal(dx)
    said = f'reported {claimed}' if claimed else 'reached a verdict'
    because = f' ({reason})' if reason else ''
    return (f'`{name}` stays Unknown: its investigation {said}, but that was not accepted{because}. '
            'Not a fault; the next scan checks it again.')


def next_step(dx):
    category = category_for(dx)
    gap = dx.get('verification_gap') or {}
    if category == 'gate_contradiction':
        return ("A review found the evidence check wrongly rejected this verdict; report it to the agent's "
                'maintainers. Make no configuration change on that verdict alone.')
    if pending_gate_review(dx):
        return refused_verdict(dx)
    defaults = {
        'access_failure': 'Resolve the named access limitation, then retry the blocked check; customer-host access requires operator follow-up.',
        'identity_missing': 'Retrieve this exact service definition and confirm its device/interface/bridge-domain or routing identifiers.',
        'forwarding_incomplete': 'Complete the named missing PE-side check while preserving verified directions; do not assume customer traffic is required.',
    }
    return gap.get('next_check') or defaults[category]


def format_unknown_breakdown(case):
    from diagnostic_mas.operator_report import _dataplane_by_name
    from diagnostic_mas.device_health import services_from_case
    rows = _dataplane_by_name(case)
    services = services_from_case(case)
    names = {str(r.get('name') or k.split('/', 1)[-1]) for k, r in services.items()}
    if services:
        rows = {k: v for k, v in rows.items() if k in names}
    unknown = {k:v for k,v in rows.items() if v.get('complete') is False or v.get('dataplane_status') == 'unknown'}
    for name, coverage in (case.service_coverage or {}).items():
        if name not in rows and (not services or name in names) and coverage in {'unresolved','investigated'}:
            unknown[name] = {}  # Coverage without a finding cannot prove readiness.
    if not unknown:
        return []
    counts = {key:0 for key in CATEGORIES}
    for dx in unknown.values():
        counts[category_for(dx)] += 1
    lines = [f"**Unknown breakdown ({len(unknown)} service{'' if len(unknown) == 1 else 's'}):**", '']
    guidance = {
        'access_failure': 'a device or endpoint the check needed could not be read; once that access works, the next scan retries',
        'identity_missing': "the service's devices or interfaces could not be confirmed from NSO; check its definition in NSO",
        'forwarding_incomplete': "the checks that ran passed, but one piece of evidence is missing (named in the service's details); not a fault",
        'gate_contradiction': "the evidence check wrongly rejected a verdict, as a review confirmed; report it to the agent's maintainers",
    }
    # Only reasons some service has: a zero would read as a statement about the whole scan.
    lines += [f'- {label}: **{counts[key]}** — {guidance[key]}.' for key,label in CATEGORIES.items() if counts[key]]
    pending = sum(pending_gate_review(dx) for dx in unknown.values())
    if pending == 1:
        lines += ['', "1 of them: the evidence check did not accept its investigation's verdict."]
    elif pending:
        lines += ['', f"{pending} of them: the evidence check did not accept their investigations' verdicts."]
    lines += ['', 'Each service is counted once. These are reasons within Unknown, not additional service statuses.', '']
    return lines
