"""Offline routing discovery visualization; never infers health from missing edges."""
from __future__ import annotations
import html
import json
import math
from pathlib import Path


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
        pos = {n: [450+385*math.cos(i*2*math.pi/len(names)),
                   330+290*math.sin(i*2*math.pi/len(names))] for i,n in enumerate(names)}
        # A convex ring keeps unrelated device markers off straight link paths.
        # Leave labels outside the ring; parallel links are curved in the renderer.
        layers[role] = dict(nodes=[dict(id=n,x=round(pos[n][0],1),y=round(pos[n][1],1),managed=n in managed, collection_errors=failures.get(n, [])) for n in names],
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
