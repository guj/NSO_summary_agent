"""Self-contained, searchable operator report and bounded channel digest."""
from __future__ import annotations
import html
import re
from agent.markdown_channels import markdown_to_html


def _plain_body(md: str) -> str:
    document = markdown_to_html(md)
    match = re.search(r'<body[^>]*>(.*?)</body>', document, re.S)
    return match.group(1) if match else document


def _body(md: str) -> str:
    parts, end = [], 0
    pattern = r"(?m)^\|[^\n]+\|\n\|[ :|\-]+\|\n(?:\|[^\n]+\|(?:\n|$))+"
    for match in re.finditer(pattern, md):
        parts.append(_plain_body(md[end:match.start()]))
        rows = match.group().strip().splitlines()
        table = []
        for n, row in enumerate(rows):
            if n == 1:
                continue
            tag = 'th' if n == 0 else 'td'
            cells = ''.join(f'<{tag}>{html.escape(c.strip())}</{tag}>' for c in row.strip('|').split('|'))
            table.append('<tr>' + cells + '</tr>')
        parts.append('<table>' + ''.join(table) + '</table>')
        end = match.end()
    parts.append(_plain_body(md[end:]))
    return ''.join(parts)


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
        lines.extend(['', '**Priority follow-up:**'] + [l[:260] for l in items[:5]])
        if len(items) > 5:
            lines.append(f"{len(items)-5} more follow-up items in the full report.")
    lines += ['', 'Full findings and evidence are in the HTML report. Customer traffic delivery was not tested by this agent.']
    return '\n'.join(lines)


def render_html_report(report: str, run_id: str) -> str:
    sections = re.split(r'^## (.+)\n', report, flags=re.M)
    nav, blocks = [], []
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
                      f'<summary>{label}{badge}</summary>{_body(text)}</details>')
            records.append(({'down':0,'degraded':1,'unknown':2,'passed':3,'basic':4}[status],j,record))
        if title == 'Services':
            records.sort()
        parts.extend(r[2] for r in records)
        opened = ' open' if title in ('Services', 'Recommended follow-up') else ''
        blocks.append(f'<details class="section" id="{ident}"{opened}><summary>{html.escape(title)}</summary>{"".join(parts)}</details>')
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NSO diagnostic report</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f7fa;color:#172536;font:15px/1.6 system-ui,sans-serif}
header{padding:24px 32px;background:#172536;color:white}header h1{margin:0;font-size:24px}
nav{position:sticky;top:0;background:white;padding:12px 24px;border-bottom:1px solid #dce2e8;display:flex;gap:16px;flex-wrap:wrap;z-index:1}a{color:#155d98}
main{max-width:1200px;margin:auto;padding:24px}.controls{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}input,select,button{font:inherit;padding:8px;border:1px solid #adb9c4;border-radius:4px}input{flex:1;min-width:220px}
.section{background:white;border:1px solid #dce2e8;margin:16px 0;padding:16px}.section>summary{font-size:20px;font-weight:650;cursor:pointer}.record{border-top:1px solid #dce2e8;padding:12px 0}.record>summary{cursor:pointer;overflow-wrap:anywhere;font-weight:600}.badge{display:block;font-weight:400;font-size:13px;color:#526071}pre{overflow:auto;background:#f1f4f7;padding:12px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:6px 10px;border-bottom:1px solid #dce2e8;text-align:left}code{overflow-wrap:anywhere}[hidden]{display:none!important}#count{color:#526071}
</style></head><body><header><h1>NSO diagnostic report</h1><div>''' + html.escape(run_id) + '''</div></header><nav>''' + ''.join(nav) + '''</nav><main>''' + _body(sections[0]) + '''
<div class="controls"><input id="search" aria-label="Search report details" placeholder="Search service ID, device, type or evidence">
<select id="status" aria-label="Service status"><option value="all">All service statuses</option><option value="down">Down</option><option value="degraded">Degraded</option><option value="unknown">Unknown / incomplete</option><option value="passed">PE-readiness passed</option><option value="basic">Basic checks only</option></select>
<button id="expand">Expand visible details</button><button id="collapse">Collapse details</button></div><p id="count" aria-live="polite"></p>''' + ''.join(blocks) + '''
</main><script>
const records=[...document.querySelectorAll('.record')], search=document.querySelector('#search'), status=document.querySelector('#status');
function filter(){let n=0;for(const r of records){r.hidden=!(r.textContent.toLowerCase().includes(search.value.toLowerCase())&&(status.value==='all'||r.dataset.service!=='true'||r.dataset.status===status.value));if(!r.hidden){n++;if(search.value)r.closest('.section').open=true}}document.querySelector('#count').textContent=n+' of '+records.length+' detail entries shown';}
search.addEventListener('input',filter);status.addEventListener('change',filter);
document.querySelector('#expand').onclick=()=>records.filter(r=>!r.hidden).forEach(r=>{r.open=true;r.closest('.section').open=true});
document.querySelector('#collapse').onclick=()=>records.forEach(r=>r.open=false);filter();
document.querySelectorAll('nav a').forEach(a=>a.onclick=()=>document.querySelector(a.getAttribute('href')).open=true);
</script></body></html>'''
