"""Offline routing discovery visualization; never infers health from missing edges."""
from __future__ import annotations
import html
import json
import math
from pathlib import Path

CENTRE = (450, 330)
RADIUS, RING = 5, 12  # a device's circle, and the dashed ring of one whose discovery is incomplete, as drawn
TOUCH = 4  # a straight link this near a device's circle reads as running through it; crossing a ring does too
GAP = 8  # the space a bent link leaves outside every device's circle or ring


def _bend(a: tuple[float, float], b: tuple[float, float], others: list[tuple[float, float, float, float]]) -> float:
    """The pull away from the ring's centre that takes the a-b curve out around the other devices.

    ``others`` holds each device as (x, y, edge, reach): the radius it is drawn to, and how near a
    straight link may pass before it reads as running through it. The template draws a link as a
    quadratic curve whose control point sits this far from the straight line's midpoint, at right
    angles to it (a and b in sorted id order). Zero when the straight line passes every device;
    otherwise the smallest pull that leaves GAP outside each device's edge.
    """
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy) or 1
    nx, ny = -dy / length, dx / length
    mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    outward = -1 if nx * (CENTRE[0] - mx) + ny * (CENTRE[1] - my) > 0 else 1

    def curve(offset: float) -> list[tuple[float, float]]:
        cx, cy = mx + nx * offset, my + ny * offset
        return [((1 - t) ** 2 * a[0] + 2 * t * (1 - t) * cx + t * t * b[0],
                 (1 - t) ** 2 * a[1] + 2 * t * (1 - t) * cy + t * t * b[1]) for t in (i / 100 for i in range(101))]

    straight = curve(0)
    if all(math.hypot(x - ox, y - oy) >= reach for ox, oy, _, reach in others for x, y in straight):
        return 0.0

    def room(offset: float) -> float:
        points = curve(offset)
        return min((math.hypot(x - ox, y - oy) - edge for ox, oy, edge, _ in others for x, y in points),
                   default=math.inf)

    offsets = [outward * 2 * step for step in range(1, 101)]
    return float(next((o for o in offsets if room(o) >= GAP), max(offsets, key=room)))


def topology_data(case: dict) -> dict:
    layers = {}
    managed = set(case.get("device_names") or [])
    for role in ("isis", "bgp"):
        payload = next((e.get("payload", {}) for e in case.get("evidence", [])
                        if e.get("kind") == "spine" and e.get("role") == role), {})
        states = {e["id"]: e.get("state", {}) for e in payload.get("operational_edges", []) if e.get("id")}
        edges, seen = [], set()
        for e in payload.get("static_edges", []):
            local, remote = e.get("local") or {}, e.get("remote") or {}
            a, b = local.get("device"), remote.get("device")
            if not a or not b or not e.get("id") or e["id"] in seen:
                continue
            seen.add(e["id"])
            edges.append(dict(a=a, b=b, id=e["id"], local=local, remote=remote,
                              state=states.get(e["id"], {"status": "unknown"})))
        failures = {}
        expected_layer = "underlay" if role == "isis" else "routing"
        for issue in case.get("issues", []):
            if issue.get("code") != "collection_error" or issue.get("layer") != expected_layer:
                continue
            message = str(issue.get("message") or "")
            # Collector messages start with the exact device name, not a substring.
            device = message.partition(":")[0].strip()
            if device not in managed:
                continue
            failures.setdefault(device, [])
            if message not in failures[device]:
                failures[device].append(message)
        if not edges and not failures:
            continue
        names = sorted(managed | {e[k] for e in edges for k in ("a", "b")})
        pos = {n: (round(CENTRE[0]+385*math.cos(i*2*math.pi/len(names)), 1),
                   round(CENTRE[1]+290*math.sin(i*2*math.pi/len(names)), 1)) for i,n in enumerate(names)}
        # On a convex ring a straight link misses unrelated devices, except between devices close together:
        # it can run through one between them, or cross its dashed ring, so it bends out around it.
        # The arc then passes behind that device's label, which keeps its white outline.
        # Labels stay outside the ring; parallel links are spread further apart in the renderer.
        marks = {n: (RING, RING) if failures.get(n) else (RADIUS, RADIUS + TOUCH) for n in names}
        bends = {}
        for e in edges:
            pair = tuple(sorted((e["a"], e["b"])))
            if pair not in bends:
                bends[pair] = _bend(pos[pair[0]], pos[pair[1]], [(*pos[n], *marks[n]) for n in names if n not in pair])
            e["bend"] = bends[pair]
        layers[role] = dict(nodes=[dict(id=n,x=pos[n][0],y=pos[n][1],managed=n in managed, collection_errors=failures.get(n, [])) for n in names],
                            edges=edges,coverage=payload.get("extra", {}).get("coverage", {}))
    return layers


def render_topology(case: dict | None) -> str:
    data = topology_data(case or {})
    if not data:
        return ""
    # Escape script delimiters in untrusted device/evidence strings.
    encoded = json.dumps(data, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    document = Path(__file__).with_name("topology_template.html").read_text().replace("__TOPOLOGY_DATA__", encoded)
    document = document.replace("let layer='isis'", "let layer=" + json.dumps(next(iter(data))))
    document = document.replace("draw();\n</script>", "for(const v of ['isis','bgp']) if(!data[v]) $(v).hidden=true; draw();\n</script>")
    document = document.replace("${g.coverage.devices_failed}", "${g.coverage.devices_failed ?? 'unrecorded'}")
    return ('<details class="section" id="routing-topology"><summary>Routing topology</summary>'
            '<p>Discovery snapshot, not final fault status. Later drill findings may supersede initial alarms; consult the report findings.</p>'
            '<iframe title="Interactive routing discovery topology" sandbox="allow-scripts" '
            'style="width:100%;height:720px;border:0;display:block" srcdoc="' + html.escape(document, quote=True) + '"></iframe>'
            '<noscript>Interactive topology requires JavaScript. Routing findings remain in the report text.</noscript></details>')
