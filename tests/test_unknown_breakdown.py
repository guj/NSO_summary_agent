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
    assert 'Review the gate' in _incomplete_dig_next_action(dx)
    dx['gate_review'] = {'outcome':'contradiction_confirmed'}
    assert category_for(dx) == 'forwarding_incomplete'
    dx['gate_review'].update(reason='Scoped evidence contradicts rejection', evidence_ids=['ev_2'])
    assert category_for(dx) == 'gate_contradiction'
    assert 'configuration changes' in next_step(dx)


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
    assert 'Gate contradiction: **0**' in text
    assert '1 of the forwarding-incomplete' in text
    assert rows['pass']['dataplane_status'] == 'up'
