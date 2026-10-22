import pytest
from nso_facts.topology.routing import BgpSessionObservation, _unpaired_session_state

EDGE={'local':{'device':'a','address':'10.0.0.1'},'remote':{'device':'b','address':'10.0.0.2'}}
@pytest.mark.parametrize('states,expected', [(['Established'], 'unknown'),(['Idle'],'down'),([], 'unknown'),(['Established','Established'],'up')])
def test_partial_evidence(states,expected):
    rows=[BgpSessionObservation('a','10.0.0.2',states[0])] if states else []
    if len(states)>1: rows.append(BgpSessionObservation('b','10.0.0.1',states[1]))
    result=_unpaired_session_state(EDGE,rows)
    assert result['status']==expected
    if states: assert result['local']==states[0].lower()
    if len(states)<2: assert result['remote']=='unknown'

def test_other_neighbor_does_not_supply_evidence():
    assert _unpaired_session_state(EDGE,[BgpSessionObservation('a','10.0.0.9','Idle')])['status']=='unknown'

@pytest.mark.asyncio
@pytest.mark.parametrize('observed,status,code',[('Established','unknown','bgp_verification_incomplete'),('Idle','down','configured_no_session')])
async def test_collector_uses_partial_state(monkeypatch, observed, status, code):
    import nso_facts.topology.routing as routing
    async def observations(*args):
        return [BgpSessionObservation('a','10.0.0.2',observed)], [], {'devices_failed':1}
    async def configs(*args): return {}
    async def ids(*args): return {'a':'10.0.0.1','b':'10.0.0.2'}
    monkeypatch.setattr(routing,'_collect_observations',observations)
    monkeypatch.setattr(routing,'_neighbor_configs_for_devices',configs)
    monkeypatch.setattr(routing,'_router_ids_for_devices',ids)
    monkeypatch.setattr(routing,'_enrich_router_ids_from_live',ids)
    edges,issues,_,_=await routing.collect_operational_routing(None,['a','b'],[dict(EDGE,id='pair')])
    assert edges[0]['state']['status']==status
    assert edges[0]['state']['local']==observed.lower()
    assert any(i['code']==code and i['edge_id']=='pair' for i in issues)
    if status=='unknown': assert not any(i['code']=='configured_no_session' for i in issues)


STAR_SEES_CIEN = [BgpSessionObservation('star-data-sw', '10.148.0.1', 'Established')]
IDS = {'star-data-sw': '10.129.128.1', 'cien-data-sw': '10.148.0.1'}


def test_no_reverse_row_is_expected_from_a_peer_whose_bgp_was_not_read():
    from nso_facts.topology.routing import pair_bgp_observations
    _, issues = pair_bgp_observations(STAR_SEES_CIEN, {}, IDS)
    assert [i['code'] for i in issues] == ['missing_reverse_session']
    # cien's table was never read, so "no matching reciprocal row" would claim more than was seen.
    _, issues = pair_bgp_observations(STAR_SEES_CIEN, {}, IDS, unread_devices={'cien-data-sw'})
    assert issues == []


@pytest.mark.asyncio
async def test_a_session_toward_an_unread_peer_stays_listed_from_the_read_end(monkeypatch):
    import nso_facts.topology.routing as routing
    unread = {'severity': 'low', 'layer': 'routing', 'code': 'collection_error', 'edge_id': None,
              'message': 'cien-data-sw: BGP session state is not exposed on this platform (...)'}
    async def observations(*args): return STAR_SEES_CIEN, [unread], {'devices_failed': 1}
    async def configs(*args): return {}
    async def ids(*args): return IDS
    monkeypatch.setattr(routing, '_collect_observations', observations)
    monkeypatch.setattr(routing, '_neighbor_configs_for_devices', configs)
    monkeypatch.setattr(routing, '_router_ids_for_devices', ids)
    monkeypatch.setattr(routing, '_enrich_router_ids_from_live', ids)
    static = {'id': 'pair', 'local': {'device': 'star-data-sw', 'address': '10.129.128.1'},
              'remote': {'device': 'cien-data-sw', 'address': '10.148.0.1'}}
    edges, issues, _, _ = await routing.collect_operational_routing(None, list(IDS), [static])
    assert not any(i['code'] == 'missing_reverse_session' for i in issues)
    assert (edges[0]['state']['local'], edges[0]['state']['remote']) == ('established', 'unknown')
    assert any(i['code'] == 'bgp_verification_incomplete' and i['edge_id'] == 'pair' for i in issues)
