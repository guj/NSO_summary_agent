from types import SimpleNamespace
import pytest
from diagnostic_mas.unknown_breakdown import category_for, next_step, format_unknown_breakdown
from diagnostic_mas.investigation import normalize_gap
from diagnostic_mas.operator_report import _incomplete_dig_next_action


@pytest.mark.parametrize('gap,expected', [
    ({'blocker':'endpoint_access_unavailable'}, 'access_failure'),
    ({'blocker':'identity_unverified'}, 'identity_missing'),
    ({'blocker':'llm_timeout'}, 'forwarding_incomplete'),
    ({'blocker':'query_error','category':'identity_missing'}, 'identity_missing'),
    ({'blocker':'gate_rejected','category':'gate_contradiction'}, 'forwarding_incomplete'),
    ({}, 'forwarding_incomplete'),
])
def test_primary_reason(gap, expected):
    assert category_for({'verification_gap':gap}) == expected


def test_gate_review_requires_evidence_and_reason():
    dx = {'source':'gate', 'verification_gap':{'next_check':'Change configuration'}}
    # A refused verdict's own suggestion never reaches the reader.
    step = _incomplete_dig_next_action(dx)
    assert 'Change configuration' not in step and 'not accepted' in step and 'not a fault' in step
    dx['gate_review'] = {'outcome':'contradiction_confirmed'}
    assert category_for(dx) == 'forwarding_incomplete'
    dx['gate_review'].update(reason='Scoped evidence contradicts rejection', evidence_ids=['ev_2'])
    assert category_for(dx) == 'gate_contradiction'
    assert 'configuration change' in next_step(dx) and 'maintainers' in next_step(dx)


def test_a_refused_up_is_explained_with_the_checks_own_reason():
    from diagnostic_mas.operator_report import _incomplete_dig_followup

    dx = {'source': 'llm',
          'verification_gap': {'blocker': 'gate_rejected',
                               'next_check': 'Collect the evidence required by the rejected conclusion before reassessing.'},
          'cause': '[gate] l2sts up refused: required bidirectional forwarding evidence is incomplete. '
                   'Preserve the verified checks and named remaining gap in the observations. (LLM claimed up: No fault)'}
    step = _incomplete_dig_next_action(dx)
    follow = _incomplete_dig_followup('link_d-1', dx)

    assert step.startswith('The investigation reported up, but that was not accepted: '
                           'required bidirectional forwarding evidence is incomplete.')
    assert follow.startswith('`link_d-1` stays Unknown: its investigation reported up, but that was not accepted '
                             '(required bidirectional forwarding evidence is incomplete).')
    for text in (step, follow):
        assert 'not a fault' in text.lower() and 'next scan' in text
        assert 'gate' not in text.lower() and 'Preserve' not in text and 'replay' not in text


def test_normalization_preserves_verified_direction_but_not_llm_gate_claim():
    gap = {'blocker':'insufficient_evidence','missing_check':'B to A forwarding',
           'next_check':'Read B forwarding for the confirmed BD',
           'verified_checks':'A to B forwarding passed', 'category':'forwarding_incomplete'}
    assert normalize_gap(gap)['verified_checks'] == gap['verified_checks']
    assert normalize_gap(gap)['category'] == gap['category']
    gap['category'] = 'gate_contradiction'
    assert 'category' not in normalize_gap(gap)


def _scan(services, digs=None, coverage=None):
    """A scan's case: l3rt services on one router each, their fixed-check status, and any LLM investigation."""
    from diagnostic_mas.case import Budget, CaseFile, add_evidence

    case = CaseFile(budget=Budget(0, 0))
    records = {}
    for name, status in services.items():
        sync = "out-of-sync" if status == "out-of-sync" else "in-sync"
        records[f"l3rt/{name}"] = {"service_type": "l3rt", "name": name, "devices": ["pe-1"],
                                   "device_sync": {"pe-1": sync},
                                   "basic_checks": {"status": "up" if status == "out-of-sync" else status}}
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services",
                        "payload": {"extra": {"services": records}}})
    for name, dx in (digs or {}).items():
        case.diagnoses.append({"kind": "dataplane", "subject": {"name": name, "service_type": "l3rt"}, **dx})
    case.service_coverage = dict(coverage or {})
    return case


def _bullets(text):
    return [line for line in text.splitlines() if line.startswith('- ')]


def test_breakdown_excludes_passes_and_counts_each_unknown_once():
    case = _scan(
        {'access': 'unknown', 'identity': 'unknown', 'gate': 'unknown', 'pass': 'unknown', 'pending': 'unknown',
         'baseline': 'up'},
        digs={'access': {'source': 'llm', 'status': 'unknown', 'complete': False,
                         'verification_gap': {'blocker': 'endpoint_access_unavailable'}},
              'identity': {'source': 'llm', 'status': 'unknown', 'verification_gap': {'blocker': 'identity_unverified'}},
              'gate': {'source': 'gate', 'status': 'unknown'},
              'pass': {'source': 'llm', 'status': 'up', 'complete': True}},
        coverage={'gate': 'unresolved', 'pending': 'investigated', 'baseline': 'basic_passed'})
    text = '\n'.join(format_unknown_breakdown(case))
    assert 'Unknown breakdown (4 services)' in text
    assert 'Access failure: **1**' in text
    assert 'Service identity missing: **1**' in text
    assert 'Forwarding evidence incomplete: **2**' in text
    assert 'Gate contradiction' not in text                 # a category with no services is not listed
    assert "1 of them: the evidence check did not accept its investigation's verdict." in text
    assert 'replay' not in text and 'pending review' not in text
    assert 'finish the missing direction' not in text       # written for the reader, not the investigator


def test_one_unknown_service_is_counted_in_the_singular():
    case = _scan({'gate': 'unknown'}, digs={'gate': {'source': 'gate', 'status': 'unknown'}})
    text = '\n'.join(format_unknown_breakdown(case))
    assert 'Unknown breakdown (1 service):' in text
    assert _bullets(text) == ["- Forwarding evidence incomplete: **1** — the checks that ran passed, but one piece of "
                              "evidence is missing (named in the service's details); not a fault."]


def test_the_breakdown_counts_exactly_the_tables_unknown_services():
    from diagnostic_mas.service_final_status import final_service_counts

    # test9 (27 Oct 2026): inference-fabnet's investigation was incomplete; test1's fixed check could not
    # confirm its routes and it was not investigated. The table counted 2 Unknown, the breakdown 1.
    case = _scan(
        {'inference-fabnet': 'unknown', 'test1': 'unknown', 'unsynced': 'out-of-sync', 'down-anyway': 'down'},
        digs={'inference-fabnet': {'source': 'llm', 'status': 'unknown', 'complete': False,
                                   'verification_gap': {'blocker': 'endpoint_access_unavailable'}},
              # An incomplete investigation keeps the fixed check's Down: the table does not call it Unknown.
              'down-anyway': {'source': 'llm', 'status': 'unknown', 'complete': False,
                              'verification_gap': {'blocker': 'llm_timeout'}}},
        coverage={'inference-fabnet': 'unresolved', 'test1': 'collection_concluded'})
    text = '\n'.join(format_unknown_breakdown(case))

    assert final_service_counts(case)['l3rt']['unknown'] == 3
    assert 'Unknown breakdown (3 services):' in text
    assert _bullets(text) == [
        "- Access failure: **1** — a device or endpoint the check needed could not be read; once that access "
        "works, the next scan retries.",
        "- Not investigated: **1** — a fixed check could not confirm the service and no LLM investigation ran "
        "this scan (its details name the check); the next scan checks it again.",
        "- Sync not confirmed: **1** — a device of the service is out of sync with NSO, or its sync state could "
        "not be read; the service is assessed once the sync is confirmed."]


def test_a_reason_worded_without_refused_is_read_too():
    from diagnostic_mas.operator_report import _incomplete_dig_followup

    # test63, CEPH_STORAGE_IPv-ca2c5a3f: the evidence check's reason, then the investigation's own claim.
    dx = {'source': 'llm', 'verification_gap': {'blocker': 'gate_rejected'},
          'cause': '[gate] the fixed checks could not read cien-data-sw; an Up needs a fixed-check reading from '
                   'every device of the service (LLM claimed up: Passed PE-side readiness checks. AC port UP.)'}
    reason = ('the fixed checks could not read cien-data-sw; an Up needs a fixed-check reading from every device '
              'of the service')
    assert _incomplete_dig_next_action(dx).startswith(f'The investigation reported up, but that was not accepted: {reason}.')
    assert _incomplete_dig_followup('ceph-1', dx).startswith(
        f'`ceph-1` stays Unknown: its investigation reported up, but that was not accepted ({reason}).')
    assert 'Passed PE-side' not in _incomplete_dig_next_action(dx)        # the investigation's claim is not the reason
