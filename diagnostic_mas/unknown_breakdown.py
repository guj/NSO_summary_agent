"""Report verification gaps without turning missing evidence into network faults."""
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


def next_step(dx):
    category = category_for(dx)
    gap = dx.get('verification_gap') or {}
    if category == 'gate_contradiction' or pending_gate_review(dx):
        return ('Review the gate decision against the cited evidence and replay the finding; '
                'do not infer a network fault or request configuration changes from rejection alone.')
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
    lines = [f'**Unknown breakdown ({len(unknown)} services):**', '']
    guidance = {
        'access_failure': 'restore access, then retry the blocked check',
        'identity_missing': 'confirm the exact service and device objects',
        'forwarding_incomplete': 'finish the missing direction/check; preserve what passed',
        'gate_contradiction': 'correct/replay the validator using reviewed evidence',
    }
    lines += [f'- {label}: **{counts[key]}** — {guidance[key]}.' for key,label in CATEGORIES.items()]
    pending = sum(pending_gate_review(dx) for dx in unknown.values())
    if pending:
        lines += ['', f'{pending} of the forwarding-incomplete services have a gate rejection pending review; these are not confirmed gate contradictions.']
    lines += ['', 'Each service is counted once. These are reasons within Dataplane Unknown, not additional service statuses. Model timeouts and unclassified query gaps remain forwarding-incomplete with their specific blocker shown in the service details.', '']
    return lines
