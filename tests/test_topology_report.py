import html
import json
import re
from diagnostic_mas.topology_report import topology_data, render_topology
from diagnostic_mas.html_report import render_html_report


def case():
    return {'device_names': ['a', 'b', 'isolated'], 'evidence': [
        {'kind': 'spine', 'role': 'bgp', 'payload': {
            'static_edges': [
                {'id': 'one', 'local': {'device': 'a', 'interface': 'x'}, 'remote': {'device': 'b'}},
                {'id': 'two', 'local': {'device': 'a', 'interface': 'y'}, 'remote': {'device': 'b'}}],
            'operational_edges': [{'id': 'one', 'state': {'status': 'up'}}]}}]}


def test_parallel_links_and_missing_state_not_inferred_up():
    data = topology_data(case())['bgp']
    assert len(data['edges']) == 2
    assert data['edges'][1]['state']['status'] == 'unknown'
    assert {n['id'] for n in data['nodes']} == {'a', 'b', 'isolated'}
    assert data == topology_data(case())['bgp']


def test_no_evidence_omits_map():
    assert render_topology({}) == ''
    assert 'id="routing-topology"' not in render_html_report('# Report', 'test')


def test_script_injection_escaped_and_single_layer_supported():
    c = case()
    c['device_names'].append('</script><script>alert(1)</script>')
    rendered = render_topology(c)
    doc = html.unescape(re.search(r'srcdoc="(.*?)"></iframe>', rendered, re.S)[1])
    assert '</script><script>alert' not in doc
    assert "let layer=\"bgp\"" in doc
    assert "if(!data[v]) $(v).hidden=true" in doc
    encoded = re.search(r'const data=(.*?);\nlet layer=', doc, re.S)[1]
    assert json.loads(encoded)['bgp']['nodes']
    assert 'sandbox="allow-scripts"' in rendered


def test_html_integration_collapsed_before_service_details():
    doc = render_html_report('# Report\n## Services\nservice text', 'test', case=case())
    assert '<details class="section" id="routing-topology"><summary>' in doc
    assert doc.index('id="routing-topology"') < doc.index('id="section-1"')
    assert 'Later drill findings may supersede' in doc


def test_collection_failures_are_protocol_specific_and_keep_edges():
    c = case()
    c['issues'] = [
        {'code':'collection_error','layer':'underlay','message':'a: ISIS command timed out'},
        {'code':'collection_error','layer':'routing','message':'b: BGP command unavailable'},
        {'code':'collection_error','layer':'routing','message':'b: BGP command unavailable'},
        {'code':'collection_error','layer':'routing','message':'ab: not device a'},
    ]
    data = topology_data(c)
    # Even when no links were collected, the failed protocol remains inspectable.
    assert 'isis' in data
    isis = {n['id']:n for n in data['isis']['nodes']}
    bgp = {n['id']:n for n in data['bgp']['nodes']}
    assert isis['a']['collection_errors'] == ['a: ISIS command timed out']
    assert not bgp['a']['collection_errors']
    assert bgp['b']['collection_errors'] == ['b: BGP command unavailable']
    assert len(data['bgp']['edges']) == 2
    assert data['bgp']['edges'][0]['state']['status'] == 'up'
    output = render_topology(c)
    assert 'Discovery incomplete' in output
    assert 'BGP command unavailable' in output
