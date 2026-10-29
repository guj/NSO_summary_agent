import html
import json
import math
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


def _ring_case(*pairs, incomplete=()):
    """34 devices on the ring, as in the live fleet, with an IS-IS link for each pair.

    Devices in ``incomplete`` had IS-IS collection fail, so the map rings them (radius 12, not 5).
    """
    names = [f'd{i:02}' for i in range(34)]
    edges = [{'id': f'{a}-{b}', 'local': {'device': a}, 'remote': {'device': b}} for a, b in pairs]
    return {'device_names': names, 'evidence': [{'kind': 'spine', 'role': 'isis', 'payload': {
        'static_edges': edges, 'operational_edges': [{'id': e['id'], 'state': {'status': 'up'}} for e in edges]}}],
        'issues': [{'code': 'collection_error', 'layer': 'underlay', 'message': f'{name}: ISIS command timed out'}
                   for name in incomplete]}


def _curve(layer, edge):
    """Points along the link as the template draws it: a quadratic curve pulled ``bend`` off the midpoint."""
    nodes = {n['id']: n for n in layer['nodes']}
    a, b = (nodes[name] for name in sorted((edge['a'], edge['b'])))
    dx, dy = b['x'] - a['x'], b['y'] - a['y']
    length = math.hypot(dx, dy)
    cx = (a['x'] + b['x']) / 2 - dy / length * edge['bend']
    cy = (a['y'] + b['y']) / 2 + dx / length * edge['bend']
    return [((1 - t) ** 2 * a['x'] + 2 * t * (1 - t) * cx + t * t * b['x'],
             (1 - t) ** 2 * a['y'] + 2 * t * (1 - t) * cy + t * t * b['y']) for t in (i / 200 for i in range(201))]


def _clearance(layer, edge):
    """Closest approach of the drawn curve to a device it does not join."""
    return min(math.hypot(x - n['x'], y - n['y']) for n in layer['nodes'] if n['id'] not in (edge['a'], edge['b'])
               for x, y in _curve(layer, edge))


def _outward(layer, edge):
    """Whether the bend pulls the curve away from the middle of the ring (450, 330)."""
    middle = sum(x for x, _ in _curve(layer, edge)) / 201, sum(y for _, y in _curve(layer, edge)) / 201
    straight = _curve(layer, dict(edge, bend=0))
    chord = sum(x for x, _ in straight) / 201, sum(y for _, y in straight) / 201
    return math.hypot(middle[0] - 450, middle[1] - 330) > math.hypot(chord[0] - 450, chord[1] - 330)


def test_a_link_bends_outward_only_when_it_would_run_through_another_device():
    # d20 and d22 sit either side of d21 (as salt and seat do of scm): a straight line runs through d21.
    layer = topology_data(_ring_case(('d20', 'd22'), ('d23', 'd26'), ('d00', 'd17'), ('d05', 'd06')))['isis']
    edges = {e['id']: e for e in layer['edges']}

    assert _clearance(layer, dict(edges['d20-d22'], bend=0)) < 6
    # Bent, it leaves 8 pixels between itself and d21's circle (radius 5).
    assert _clearance(layer, edges['d20-d22']) >= 5 + 8
    # d23 to d26 passes about 6 pixels outside the circles of d24 and d25: clear, so straight.
    assert edges['d23-d26']['bend'] == 0 and _clearance(layer, edges['d23-d26']) > 5 + 4
    # A ringed device (discovery incomplete, radius 12) at the live positions: d24 salt, d25 scm, d26 seat,
    # d27 sri, d28 star. A link crossing the ring bends and clears it; salt to star passes outside it, straight.
    ringed = topology_data(_ring_case(('d24', 'd26'), ('d24', 'd27'), ('d24', 'd28'), incomplete=['d25']))['isis']
    links = {e['id']: e for e in ringed['edges']}
    assert _clearance(ringed, links['d24-d26']) >= 12 + 8 and _clearance(ringed, links['d24-d27']) >= 12 + 8
    assert links['d24-d28']['bend'] == 0 and _clearance(ringed, links['d24-d28']) > 12
    # It bends out around the device, away from the middle of the ring, and stays inside the drawing,
    # even at the top of the ring past a ringed device.
    doc = html.unescape(re.search(r'srcdoc="(.*?)"></iframe>', render_topology(_ring_case(('d20', 'd22'))), re.S)[1])
    left, top, width, height = map(float, re.search(r'<svg id="map" viewBox="([^"]+)"', doc)[1].split())
    for lay, link in ((layer, edges['d20-d22']), (ringed, links['d24-d26']), (ringed, links['d24-d27'])):
        assert _outward(lay, link)
        assert all(left <= x <= left + width and top <= y <= top + height for x, y in _curve(lay, link))
    # A line across the ring, or between next-door devices, is already clear and stays straight.
    assert edges['d00-d17']['bend'] == 0 and edges['d05-d06']['bend'] == 0
    assert '*20+(e.bend||0);' in doc


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
