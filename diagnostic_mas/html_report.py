"""Self-contained, searchable operator report and bounded channel digest."""
from __future__ import annotations
import html
import json
import re
from agent.markdown_channels import markdown_to_html


def _plain_body(md: str) -> str:
    document = markdown_to_html(md)
    match = re.search(r'<body[^>]*>(.*?)</body>', document, re.S)
    return match.group(1) if match else document


STATUS_DEFINITIONS_HTML = '<details class="status-definitions"><summary>Status definitions</summary>\n<p><strong>Configuration sync</strong> — endpoint configuration, independent of dataplane health.</p>\n<ul><li><strong>In:</strong> All service endpoints report in-sync.</li>\n<li><strong>Out:</strong> At least one endpoint reports out-of-sync.</li>\n<li><strong>Unknown:</strong> No endpoint is confirmed out-of-sync, but at least one endpoint’s sync result is unavailable (or endpoint evidence is missing).</li></ul>\n<p><strong>Dataplane (PE readiness)</strong></p>\n<ul><li><strong>Up:</strong> Required PE-side checks passed. Customer traffic delivery was not tested.</li>\n<li><strong>Down:</strong> Evidence confirms a failed component or forwarding path required by the service.</li>\n<li><strong>Degraded:</strong> Evidence confirms partial impairment while some service functionality remains available.</li>\n<li><strong>Unknown:</strong> Investigation was attempted but evidence is insufficient or contradictory. A timeout alone does not mean Down.</li>\n<li><strong>Not checked:</strong> No dataplane investigation was performed this run.</li></ul>\n</details>'


def _body(md: str) -> str:
    from diagnostic_mas.service_final_status import MARKER, STATUSES
    breakdown = {}
    def extract(match):
        nonlocal breakdown
        try:
            value = json.loads(match[1])
            if isinstance(value, dict):
                breakdown = value
        except (ValueError, TypeError):
            pass
        return ""
    md = re.sub(r"<!-- " + re.escape(MARKER) + r"(.*?) -->", extract, md, flags=re.S)
    parts, end = [], 0
    pattern = r"(?m)^\|[^\n]+\|\n\|[ :|\-]+\|\n(?:\|[^\n]+\|(?:\n|$))+"
    for match in re.finditer(pattern, md):
        parts.append(_plain_body(md[end:match.start()]))
        rows = match.group().strip().splitlines()
        table = []
        headers = [c.strip() for c in rows[0].strip('|').split('|')]
        final_table = headers == ["Service type", "Total", "OpUp", "Down", "Degraded", "Unknown"]
        grouped = headers == ['service', 'Total', 'Sync In', 'Sync Out', 'Sync Unknown',
                              'DP Up', 'DP Down', 'DP Degraded', 'DP Unknown', 'DP Not checked']
        if grouped:
            table.append('<thead><tr><th rowspan="2" scope="col">Service</th>'
                         '<th rowspan="2" scope="col">Total</th>'
                         '<th colspan="3" scope="colgroup">Configuration sync</th>'
                         '<th colspan="5" scope="colgroup">Dataplane (PE readiness)</th></tr><tr>'
                         + ''.join('<th scope="col">' + label + '</th>' for label in
                                   ['In', 'Out', 'Unknown', 'Up', 'Down', 'Degraded', 'Unknown', 'Not checked'])
                         + '</tr></thead><tbody>')
        for n, row in enumerate(rows):
            if n == 1 or (grouped and n == 0):
                continue
            tag = 'th' if n == 0 else 'td'
            values = [c.strip() for c in row.strip('|').split('|')]
            cells = ''
            for index, value in enumerate(values):
                cell = html.escape(value)
                if final_table and n > 1 and index >= 2:
                    record = breakdown.get(html.unescape(values[0]), {})
                    sources = record.get("sources", {}).get(STATUSES[index-2], {}) if index < 6 else {}
                    if sources and sum(sources.values()) == int(value):
                        entries = ''.join('<li>' + html.escape(str(label)) + ': ' + str(int(count)) + '</li>' for label,count in sources.items())
                        cell = '<details class="status-count"><summary aria-label="' + html.escape(values[0] + ' ' + headers[index] + ' breakdown', quote=True) + '">' + cell + '</summary><ul>' + entries + '</ul></details>'
                cells += f'<{tag}>{cell}</{tag}>'

            table.append('<tr>' + cells + '</tr>')
        parts.append('<div style="overflow-x:auto"><table>' + ''.join(table) + ('</tbody>' if grouped else '') + '</table></div>')
        if grouped:
            parts.append(STATUS_DEFINITIONS_HTML)
        elif final_table:
            parts.append('<details class="status-definitions"><summary>Status definitions</summary><ul><li><strong>OpUp:</strong> Required PE-side operational checks passed, either by basic checks or a supported LLM conclusion. Customer delivery was not tested.</li><li><strong>Down:</strong> A required service component or path is confirmed failed.</li><li><strong>Degraded:</strong> Confirmed partial impairment.</li><li><strong>Unknown:</strong> Sync prerequisite failed, or operational evidence is insufficient.</li></ul><p>An incomplete LLM dig preserves a confirmed operational fault. Each service belongs to exactly one final-status cell.</p></details>')
        end = match.end()
    parts.append(_plain_body(md[end:]))
    return ''.join(parts)


def _device_details(case: dict | None) -> dict[str, str]:
    """Collapsed interface and hardware detail per device, built from the saved case."""
    if not isinstance(case, dict):
        return {}
    from types import SimpleNamespace

    from diagnostic_mas.device_health import device_detail_lines

    view = SimpleNamespace(
        evidence=case.get("evidence") or [],
        device_names=case.get("device_names") or [],
        focus_devices=case.get("focus_devices") or [],
    )
    details = device_detail_lines(view, live_verified=case.get("live_verified_devices"))
    return {
        name: '<details class="device-detail"><summary>Interfaces and hardware</summary><pre>'
        + html.escape("\n".join(lines))
        + "</pre></details>"
        for name, lines in details.items()
    }


def notification_digest(report: str, run_id: str) -> str:
    lines = [f"# NSO diagnostic report — {run_id}"]
    for prefix in ("**Scope:**", "**Duration:**"):
        line = next((l for l in report.splitlines() if l.startswith(prefix)), "")
        if line:
            lines.append(line[:200])
    coverage = next((l for l in report.splitlines() if 'Only ' in l and 'received additional dataplane investigation:' in l), '')
    if coverage:
        lines.append(coverage[:850])
    else:
        result = next((l for l in report.splitlines() if l.startswith('**Result:**')), '')
        lines.append(result[:850])
    section = re.search(r'^## Recommended follow-up\s*\n(.*?)(?=^## |\Z)', report, re.M | re.S)
    if section:
        items = re.findall(r'^\d+\. .+', section.group(1), re.M)
        lines.extend(['', '**Priority follow-up:**'] + [_clip(l, 260) for l in items[:5]])
        if len(items) > 5:
            lines.append(f"{len(items)-5} more follow-up items in the full report.")
    lines += ['', 'Full findings and evidence are in the HTML report. Customer traffic delivery was not tested by this agent.']
    return '\n'.join(lines)


def _clip(line: str, limit: int) -> str:
    """Shorten a line between words, never inside a `code` span."""
    if len(line) <= limit:
        return line
    cut = line[:limit - 2].rsplit(' ', 1)[0]
    if cut.count('`') % 2:
        cut = cut[:cut.rfind('`')]
    cut = cut.rstrip(' ,;')
    if cut.endswith(':') and '. ' in cut:
        # A list label whose entries were all cut says nothing; end the sentence before it.
        cut = cut[:cut.rfind('. ') + 1]
    return cut.rstrip(' ,;:') + ' …'


def render_html_report(report: str, run_id: str, *, case: dict | None = None) -> str:
    sections = re.split(r'^## (.+)\n', report, flags=re.M)
    from diagnostic_mas.topology_report import render_topology
    topology = render_topology(case)
    from diagnostic_mas.service_topology import render_service_topology
    service_topology = render_service_topology(case)
    device_details = _device_details(case)
    nav, blocks = [], []
    if service_topology:
        nav.append('<a href="#service-topology">Service topology</a>')
    if topology:
        nav.append('<a href="#routing-topology">Routing topology</a>')
    for i in range(1, len(sections), 2):
        title, content = sections[i], sections[i+1]
        ident = f'section-{i}'
        nav.append(f'<a href="#{ident}">{html.escape(title)}</a>')
        chunks = re.split(r'^### (.+)\n', content, flags=re.M)
        parts = [_body(chunks[0])]
        records = []
        for j in range(1, len(chunks), 2):
            name, text = chunks[j], chunks[j+1]
            result = re.search(r'^\*\*Result:\*\* (.+)', text, re.M)
            result = result.group(1) if result else ''
            lower = result.lower()
            status = ('down' if 'confirmed fault' in lower and 'down' in lower else
                      'degraded' if 'degraded' in lower else
                      'unknown' if 'incomplete' in lower or 'unknown' in lower else
                      'passed' if 'passed pe-side' in lower else 'basic')
            label = html.escape(name)
            badge = f'<span class="badge">{html.escape(result)}</span>' if result else ''
            record = (f'<details class="record" data-status="{status}" data-service="{str(title == "Services").lower()}">'
                      f'<summary>{label}{badge}</summary>{_body(text)}'
                      f'{device_details.get(name.strip(), "") if title == "Devices" else ""}</details>')
            records.append(({'down':0,'degraded':1,'unknown':2,'passed':3,'basic':4}[status],j,record))
        if title == 'Services':
            records.sort()
        parts.extend(r[2] for r in records)
        opened = ' open' if title in ('Summary', 'Services', 'Recommended follow-up') else ''
        blocks.append(f'<details class="section" id="{ident}"{opened}><summary>{html.escape(title)}</summary>{"".join(parts)}</details>')
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NSO diagnostic report</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f7fa;color:#172536;font:15px/1.6 system-ui,sans-serif}
header{padding:24px 32px;background:#172536;color:white}header h1{margin:0;font-size:24px}
nav{position:sticky;top:0;background:white;padding:12px 24px;border-bottom:1px solid #dce2e8;display:flex;gap:16px;flex-wrap:wrap;z-index:1}a{color:#155d98}
main{max-width:1200px;margin:auto;padding:24px}.controls{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}input,select,button{font:inherit;padding:8px;border:1px solid #adb9c4;border-radius:4px}input{flex:1;min-width:220px}
.section{background:white;border:1px solid #dce2e8;margin:16px 0;padding:16px}.section>summary{font-size:20px;font-weight:650;cursor:pointer}.record{border-top:1px solid #dce2e8;padding:12px 0}.record>summary{cursor:pointer;overflow-wrap:anywhere;font-weight:600}
.device-detail{margin-top:8px}.device-detail>summary{font-size:14px;font-weight:600;cursor:pointer}.badge{display:block;font-weight:400;font-size:13px;color:#526071}pre{overflow:auto;background:#f1f4f7;padding:12px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:6px 10px;border-bottom:1px solid #dce2e8;text-align:left}.status-definitions{margin:12px 0;font-size:14px}.status-definitions>summary{cursor:pointer;font-weight:600}code{overflow-wrap:anywhere}[hidden]{display:none!important}#count{color:#526071}
</style></head><body><header><h1>NSO diagnostic report</h1><div>''' + html.escape(run_id) + '''</div></header><nav>''' + ''.join(nav) + '''</nav><main>''' + _body(sections[0]) + '''
<div class="controls"><input id="search" aria-label="Search report details" placeholder="Search service ID, device, type or evidence">
<select id="status" aria-label="Service status"><option value="all">All service statuses</option><option value="down">Down</option><option value="degraded">Degraded</option><option value="unknown">Unknown / incomplete</option><option value="passed">PE-readiness passed</option><option value="basic">Basic checks only</option></select>
<button id="expand">Expand visible details</button><button id="collapse">Collapse details</button></div><p id="count" aria-live="polite"></p>''' + topology + service_topology + ''.join(blocks) + '''
</main><script>
window.addEventListener('message',event=>{const frame=document.querySelector('#routing-topology iframe');if(frame&&event.source===frame.contentWindow&&event.data?.type==='nso-topology-height'&&Number.isFinite(event.data.height)){frame.style.height=Math.max(200,Math.min(3000,event.data.height))+'px';}});
const records=[...document.querySelectorAll('.record')], search=document.querySelector('#search'), status=document.querySelector('#status');
function filter(){let n=0;for(const r of records){r.hidden=!(r.textContent.toLowerCase().includes(search.value.toLowerCase())&&(status.value==='all'||r.dataset.service!=='true'||r.dataset.status===status.value));if(!r.hidden){n++;if(search.value)r.closest('.section').open=true}}document.querySelector('#count').textContent=n+' of '+records.length+' detail entries shown';}
search.addEventListener('input',filter);status.addEventListener('change',filter);
document.querySelector('#expand').onclick=()=>records.filter(r=>!r.hidden).forEach(r=>{r.open=true;r.closest('.section').open=true});
document.querySelector('#collapse').onclick=()=>records.forEach(r=>r.open=false);filter();
document.querySelectorAll('nav a').forEach(a=>a.onclick=()=>document.querySelector(a.getAttribute('href')).open=true);
</script></body></html>'''
