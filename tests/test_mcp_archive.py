import json

from nso_facts.mcp_archive import archive_mcp_results, record_mcp_result


def test_archive_is_opt_in_and_scoped(tmp_path):
    with archive_mcp_results(None, 'disabled') as path:
        assert path is None
        record_mcp_result('exec_show', {}, source='wire', response='not saved')
    assert list(tmp_path.iterdir()) == []
    with archive_mcp_results(tmp_path, 'enabled') as path:
        record_mcp_result('exec_show', {}, source='wire', response='saved')
    before = path.read_text()
    record_mcp_result('exec_show', {}, source='wire', response='outside run')
    assert path.read_text() == before
    assert json.loads(before)['response'] == 'saved'
    with archive_mcp_results(tmp_path, 'enabled') as second:
        assert second != path


def test_cli_archive_does_not_enable_publish():
    from diagnostic_mas.run import build_parser
    args = build_parser().parse_args(['--dry-run', '--save-mcp-results'])
    assert args.save_mcp_results and args.dry_run
    assert not args.publish
    assert not build_parser().parse_args([]).save_mcp_results
