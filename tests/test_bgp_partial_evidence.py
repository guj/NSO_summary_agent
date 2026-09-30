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
    edges,issues,_=await routing.collect_operational_routing(None,['a','b'],[dict(EDGE,id='pair')])
    assert edges[0]['state']['status']==status
    assert edges[0]['state']['local']==observed.lower()
    assert any(i['code']==code and i['edge_id']=='pair' for i in issues)
    if status=='unknown': assert not any(i['code']=='configured_no_session' for i in issues)
