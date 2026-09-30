import json
from pathlib import Path
import pytest
from diagnostic_mas.dataplane_verify import (
    _l2sts_admits_no_type2_either_direction,
    _l2sts_up_admits_incomplete_bidirectional_proof,
    accept_dataplane_conclusion,
)


@pytest.mark.parametrize('identifier', ['Hu0/0/0/4.0', 'Hu0/0/0/4.100', 'BE100.0', '10.0.0.0', 'bd-service-0', '10', '100'])
def test_identifier_or_nonzero_count_is_not_zero_macs(identifier):
    assert not _l2sts_admits_no_type2_either_direction(
        f'both pes have observations: {identifier} mac entries'.lower())


@pytest.mark.parametrize('text', ['Both PEs have 0 MAC addresses.', 'MAC tables: 0 MACs on both endpoints.',
                                 'Both PEs: (0 MAC entries).', 'Zero MACs on both PEs.',
                                 'No type-2 routes in either direction.'])
def test_real_missing_mac_evidence_still_rejected(text):
    assert _l2sts_up_admits_incomplete_bidirectional_proof(text)


def test_nso34_real_conclusion_survives_full_gate():
    fixture=json.loads((Path(__file__).parent/'fixtures/l2sts_nso34_gate_replay.json').read_text())
    finding=fixture['finding']
    assert _l2sts_up_admits_incomplete_bidirectional_proof(finding['observed']+'\n'+finding['cause']) is None
    result=accept_dataplane_conclusion(fixture['record'],finding,session_evidence=fixture['session_evidence'])
    assert result['dataplane_status']=='up'
    assert '[gate]' not in result['cause']


def test_one_verified_direction_preserved_when_gate_rejects_up():
    observed = (
        'Both PEs have ACs and EVI up. gatech to clem MAC installation verified. '
        'clem has no type-2 locally learned MACs; clem to gatech forwarding '
        'installation remains unverified, with only IMET exchange confirmed.'
    )
    gap = {'verified_checks': 'gatech to clem MAC installation',
           'missing_check': 'clem to gatech forwarding installation',
           'direction': 'clem to gatech'}
    finding = {'dataplane_status':'up', 'observed':observed,
               'cause':'PE-side readiness claimed on both PEs.',
               'verification_gap':gap, 'fix_suggestion':'No repair'}
    fixture = json.loads((Path(__file__).parent/'fixtures/l2sts_nso34_gate_replay.json').read_text())
    result = accept_dataplane_conclusion(fixture['record'], finding,
                                        session_evidence=fixture['session_evidence'])
    assert result['dataplane_status'] == 'unknown'
    assert result['complete'] is False
    assert observed in result['observed']
    assert result['verification_gap'] == gap
    assert 'in either direction' not in result['cause']
    assert 'which direction lacks evidence' in result['cause']
    assert finding['dataplane_status'] == 'up'


def test_replication_evidence_wording_is_not_mechanically_mac_gated():
    # Synthetic model conclusions: this gate checks prose, not raw CLI truth.
    text = (
        'ACs, BD/EVI and required transport checks pass. A to B: remote MAC '
        'installed in B service BD. B to A: programmed replication entry in '
        'B service BD lists peer A operational with resolved egress; A delivery '
        'binding to the local AC is installed and operational. This establishes '
        'the required unknown-unicast/BUM readiness; learned-unicast distribution '
        'in the reverse direction and customer delivery were not tested.'
    )
    assert _l2sts_up_admits_incomplete_bidirectional_proof(text) is None


def test_imet_and_no_local_mac_story_still_rejected():
    text = (
        'Both PEs have ACs up; A to B remote MAC installed. B has no local MACs. '
        'Reverse has IMET and pseudo-port up only. Missing reverse MACs are '
        'not an import failure because the peer has no locally learned MACs.'
    )
    assert _l2sts_up_admits_incomplete_bidirectional_proof(text) is not None


@pytest.mark.parametrize('command', [
    'evpn evi vpn-id 9070 inclusive-multicast detail',
    'evpn evi vpn-id 9070 detail',
    'show evpn evi vpn-id 9082 inclusive-multicast detail',
])
def test_verified_evpn_detail_commands_allowed(command):
    from diagnostic_mas.dataplane_verify import disallowed_dataplane_show_command
    assert disallowed_dataplane_show_command(command) is None

@pytest.mark.parametrize('command', [
    'evpn evi 9070', 'l2vpn evpn evi 9070', 'run evpn',
    'evpn evi vpn-id 9070 guessed-detail',
    'evpn evi vpn-id 9070 detail ; clear bgp',
])
def test_unverified_evpn_forms_stay_blocked(command):
    from diagnostic_mas.dataplane_verify import disallowed_dataplane_show_command
    assert disallowed_dataplane_show_command(command)
