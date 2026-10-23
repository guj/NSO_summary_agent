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


def test_breakdown_excludes_passes_and_not_checked_and_counts_once(monkeypatch):
    from diagnostic_mas import operator_report, device_health
    rows = {
      'access': {'complete':False,'verification_gap':{'blocker':'endpoint_access_unavailable'}},
      'identity': {'dataplane_status':'unknown','verification_gap':{'blocker':'identity_unverified'}},
      'gate': {'dataplane_status':'unknown','source':'gate'},
      'pass': {'dataplane_status':'up','complete':True},
      'outside': {'dataplane_status':'unknown'},
    }
    monkeypatch.setattr(operator_report, '_dataplane_by_name', lambda case: rows)
    names = ['access','identity','gate','pass','pending','not_checked']
    monkeypatch.setattr(device_health, 'services_from_case', lambda case: {n:{'name':n} for n in names})
    case = SimpleNamespace(service_coverage={'gate':'unresolved','pending':'investigated','not_checked':'baseline_only'})
    text = '\n'.join(format_unknown_breakdown(case))
    assert 'Unknown breakdown (4 services)' in text
    assert 'Access failure: **1**' in text
    assert 'Service identity missing: **1**' in text
    assert 'Forwarding evidence incomplete: **2**' in text
    assert 'Gate contradiction' not in text                 # a category with no services is not listed
    assert "1 of them: the evidence check did not accept its investigation's verdict." in text
    assert 'replay' not in text and 'pending review' not in text
    assert 'finish the missing direction' not in text       # written for the reader, not the investigator
    assert rows['pass']['dataplane_status'] == 'up'


def test_one_unknown_service_is_counted_in_the_singular(monkeypatch):
    from diagnostic_mas import operator_report, device_health
    monkeypatch.setattr(operator_report, '_dataplane_by_name',
                        lambda case: {'gate': {'dataplane_status': 'unknown', 'source': 'gate'}})
    monkeypatch.setattr(device_health, 'services_from_case', lambda case: {'gate': {'name': 'gate'}})
    text = '\n'.join(format_unknown_breakdown(SimpleNamespace(service_coverage={})))
    assert 'Unknown breakdown (1 service):' in text
    bullets = [line for line in text.splitlines() if line.startswith('- ')]
    assert bullets == ["- Forwarding evidence incomplete: **1** — the checks that ran passed, but one piece of "
                       "evidence is missing (named in the service's details); not a fault."]


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
