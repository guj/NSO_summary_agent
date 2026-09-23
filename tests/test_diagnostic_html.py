from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import json
import pytest
from diagnostic_mas.html_report import render_html_report, notification_digest
from test_diagnostic_mas_publish import _settings
from agent.publish import publish_all, publish_email, publish_slack_file

REPORT = '''# NSO Diagnostic Report
**Scope:** 2 devices · 2 services
**Duration:** 1 min
## Services
| service | Total | SystemUp |
| --- | --- | --- |
| l2ptp | 2 | 1 |
### L2PTP · good
**Result:** Passed PE-side readiness checks.
### L2PTP · bad
**Result:** Confirmed fault — dataplane down
<script>alert("bad")</script>
## Recommended follow-up
1. Investigate bad.
## Run details
Many details.
'''


def test_html_preserves_details_safely_and_orders_faults():
    doc = render_html_report(REPORT, '<run>')
    assert '<script>alert' not in doc
    assert '&lt;script&gt;' in doc
    assert '<table>' in doc and '<th>SystemUp</th>' in doc
    assert doc.index('L2PTP · bad') < doc.index('L2PTP · good')
    assert 'data-status="down"' in doc and 'data-status="passed"' in doc
    assert 'https://' not in doc
    digest = notification_digest(REPORT * 100, 'run')
    assert len(digest) < 3000 and 'Many details' not in digest


def test_email_has_short_body_and_full_attachment(tmp_path):
    settings = _settings(state_dir=tmp_path)
    settings=replace(settings, email_to=['operator@example.test'], email_from='agent@example.test', smtp_host='smtp.example.test')
    path=tmp_path/'report.html'; path.write_text(render_html_report(REPORT,'run'))
    with patch('agent.publish._send_via_smtp') as send:
        publish_email('Brief summary', 'run', settings, attachment_path=path)
    msg=send.call_args.args[1]
    assert msg.get_body(preferencelist=('plain',)).get_content().strip() == 'Brief summary'
    attachments=list(msg.iter_attachments())
    assert len(attachments)==1
    assert attachments[0].get_content_type()=='text/html'
    assert attachments[0].get_payload(decode=True)==path.read_bytes()


def test_webhook_only_does_not_claim_attachment(tmp_path):
    settings=_settings(state_dir=tmp_path)
    path=tmp_path/'report.html'; path.write_text('full')
    with patch('agent.publish.publish_slack') as send:
        assert publish_all('Brief', 'run', settings, attachment_path=path)==['slack']
    assert 'cannot attach' in send.call_args.args[0]
    with patch('agent.publish.publish_slack') as send:
        publish_all('Brief', 'run', settings, attachment_path=path, report_url='https://internal/run/report.html')
    assert 'https://internal/run/report.html' in send.call_args.args[0]


def test_slack_upload_flow(tmp_path):
    settings=replace(_settings(state_dir=tmp_path), slack_bot_token='test-token', slack_channel_id='C123')
    path=tmp_path/'report.html';path.write_text('full report')
    class Response:
        def __init__(self,data): self.data=data
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return self.data
    responses=[Response(json.dumps({'ok':True,'file_id':'F1','upload_url':'https://files.slack.com/upload/x'}).encode()),Response(b'OK'),Response(b'{"ok":true}')]
    with patch('agent.publish.urllib.request.urlopen',side_effect=responses) as request:
        publish_slack_file(path,'Brief',settings)
    assert request.call_count==3
    assert request.call_args_list[1].args[0].data==path.read_bytes()
    assert request.call_args_list[1].args[0].get_header('Authorization') is None
    assert b'channel_id=C123' in request.call_args_list[2].args[0].data
    with patch('agent.publish.urllib.request.urlopen',return_value=Response(b'{"ok":false,"error":"missing_scope"}')):
        with pytest.raises(RuntimeError,match='missing_scope'):
            publish_slack_file(path,'Brief',settings)
