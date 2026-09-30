from types import SimpleNamespace
from dataclasses import replace
from diagnostic_mas.case import Budget, CaseFile
from diagnostic_mas.run_configuration import capture_run_configuration, format_run_configuration
from diagnostic_mas.state_paths import case_to_dict
from diagnostic_mas.html_report import render_html_report
from test_diagnostic_mas_publish import _settings


def test_snapshot_redacts_secrets_and_preserves_effective_values(tmp_path):
    settings = replace(_settings(state_dir=tmp_path), fabric_api_url='https://user:secret@example.org/v1?token=secret',
                       fabric_api_key='secret', mcp_server_args=['--nso-timeout=30', '--nso-password=secret'])
    args = SimpleNamespace(max_dataplane_per_category=20, max_dataplane_services=None, full=True)
    case = CaseFile(budget=Budget(0, 0))
    case.run_configuration = capture_run_configuration(args, settings, case.budget, skip_llm=True, dry_run=True)
    snapshot = case_to_dict(case)['run_configuration']
    assert snapshot['LLM provider host'] == 'example.org'
    assert snapshot['LLM enabled'] is False
    assert snapshot['Dataplane services per category'] == 20
    assert snapshot['NSO request timeout passed to MCP (seconds)'] == 30
    assert 'secret' not in str(snapshot)
    args.max_dataplane_per_category = 5
    assert snapshot['Dataplane services per category'] == 20
    html = render_html_report('\n'.join(format_run_configuration(case)), 'test')
    assert '<summary>Run configuration</summary>' in html
    assert '<details class="section" id="section-1"><summary>Run configuration' in html


def test_old_case_does_not_substitute_current_settings():
    assert 'Not recorded for this run' in '\n'.join(format_run_configuration(SimpleNamespace()))


def test_temperature_snapshot_and_html(tmp_path):
    for temperature in (None, 0.0, 1.0):
        settings = replace(_settings(state_dir=tmp_path), fabric_temperature=temperature)
        case = CaseFile(budget=Budget(0, 0))
        case.run_configuration = capture_run_configuration(
            SimpleNamespace(), settings, case.budget, skip_llm=False, dry_run=True)
        expected = "Provider default (parameter omitted)" if temperature is None else temperature
        assert case_to_dict(case)['run_configuration']['LLM temperature'] == expected
        report = '\n'.join(format_run_configuration(case))
        assert f"**LLM temperature:** {expected}" in report
        html = render_html_report(report, 'temperature-test')
        assert 'LLM temperature' in html and str(expected) in html
        assert '<details class="section" id="section-1"><summary>Run configuration' in html
