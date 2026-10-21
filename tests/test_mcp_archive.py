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


def _archive(directory, name):
    path = directory / name
    path.write_text('{}\n')
    return path


def test_a_saving_scan_first_removes_its_own_archives_older_than_the_limit(tmp_path, capsys):
    old = _archive(tmp_path, '20261012T235959Z-8n6m599c.jsonl')
    week = _archive(tmp_path, '20261013T120000Z-_lap1gnd.jsonl')
    recent = _archive(tmp_path, '20261019T200118Z-01__d555.jsonl')
    (tmp_path / 'keep').mkdir()
    kept = _archive(tmp_path / 'keep', '20261001T000000Z-abcd1234.jsonl')
    others = [_archive(tmp_path, name) for name in
              ('notes.jsonl', '20261001T000000Z.jsonl', '20261001T000000Z-abcd1234.jsonl.gz')]
    outside = _archive(tmp_path / 'keep', 'target.jsonl')
    link = tmp_path / '20261001T000000Z-linkabcd.jsonl'
    link.symlink_to(outside)

    with archive_mcp_results(tmp_path, '20261020T120000Z', keep_days=7) as path:
        pass

    # Age comes from the run ID in the name; exactly seven days old is kept.
    assert not old.exists()
    assert week.exists() and recent.exists() and path.exists()
    # Only archive files directly in the folder: keep/, other names and links are left alone.
    assert kept.exists() and all(p.exists() for p in others) and link.is_symlink()
    assert '[mcp archive] Removed 1 archive older than 7 days' in capsys.readouterr().err


def test_archives_are_kept_when_the_limit_is_zero_or_nothing_is_saved(tmp_path, capsys):
    old = _archive(tmp_path, '20261001T000000Z-8n6m599c.jsonl')
    with archive_mcp_results(None, '20261020T120000Z', keep_days=7):
        pass
    with archive_mcp_results(tmp_path, '20261020T120000Z', keep_days=0):
        pass
    assert old.exists()
    assert 'Removed' not in capsys.readouterr().err


def test_an_archive_that_cannot_be_removed_does_not_stop_the_scan(tmp_path, capsys, monkeypatch):
    from pathlib import Path

    old = _archive(tmp_path, '20261001T000000Z-8n6m599c.jsonl')

    def refuse(self, *args, **kwargs):
        raise PermissionError('read-only')

    monkeypatch.setattr(Path, 'unlink', refuse)
    with archive_mcp_results(tmp_path, '20261020T120000Z', keep_days=7) as path:
        record_mcp_result('exec_show', {}, source='wire', response='saved')
    assert old.exists() and json.loads(path.read_text())['response'] == 'saved'
    assert f'could not remove {old.name}' in capsys.readouterr().err
