"""--profile FILE: a scan's options kept in a file, with options typed on the command line winning."""
from types import SimpleNamespace

import pytest

from diagnostic_mas import run as run_mod
from diagnostic_mas.case import Budget
from diagnostic_mas.run_configuration import capture_run_configuration
from test_diagnostic_mas_publish import _settings

MAC = """\
# Full published scan, as run on the Mac

--spine-concurrent-devices 6
--dataplane-concurrent-workers 2
--max-dataplane-tools 40   # per service investigation
--publish
"""


def _profile(tmp_path, text=MAC, name="used_on_mac"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_a_scan_started_with_a_profile_runs_with_its_options(tmp_path, monkeypatch):
    seen = {}

    async def scan(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(run_mod, "_run", scan)
    assert run_mod.main(["--profile", _profile(tmp_path)]) == 0
    args = seen["args"]
    assert (args.spine_concurrent_devices, args.dataplane_concurrent_works,
            args.max_dataplane_tools, args.publish) == (6, 2, 40, True)


def test_an_option_typed_on_the_command_line_wins_wherever_it_is_typed(tmp_path):
    path = _profile(tmp_path)
    assert run_mod.parse_cli(["--profile", path, "--max-dataplane-tools", "5"]).max_dataplane_tools == 5
    assert run_mod.parse_cli(["--max-dataplane-tools", "5", f"--profile={path}"]).max_dataplane_tools == 5


def test_dry_run_on_the_command_line_replaces_the_profiles_publish(tmp_path):
    args = run_mod.parse_cli(["--profile", _profile(tmp_path), "--dry-run"])
    assert (args.dry_run, args.publish) == (True, False)


def test_an_unknown_option_in_a_profile_stops_the_run_naming_its_line(tmp_path, capsys):
    path = _profile(tmp_path, "--publish\n--max-dataplane-tool 40\n")
    with pytest.raises(SystemExit) as stop:
        run_mod.parse_cli(["--profile", path])
    err = capsys.readouterr().err
    assert stop.value.code == 2
    assert f"profile {path} line 2" in err and "--max-dataplane-tool" in err


def test_a_missing_profile_stops_the_run(tmp_path, capsys):
    path = str(tmp_path / "nowhere")
    with pytest.raises(SystemExit) as stop:
        run_mod.parse_cli(["--profile", path])
    assert stop.value.code == 2 and path in capsys.readouterr().err


def test_a_profile_cannot_name_another_profile(tmp_path, capsys):
    outer = _profile(tmp_path, f"--profile {_profile(tmp_path)}\n", name="outer")
    with pytest.raises(SystemExit) as stop:
        run_mod.parse_cli(["--profile", outer])
    assert stop.value.code == 2 and f"profile {outer} line 1" in capsys.readouterr().err


def test_run_configuration_names_the_profile_and_lists_its_options(tmp_path):
    path = _profile(tmp_path)
    args = run_mod.parse_cli(["--profile", path, "--dry-run"])
    config = capture_run_configuration(args, _settings(state_dir=tmp_path), Budget(0, 0),
                                       skip_llm=True, dry_run=True)
    assert config["Profile"] == (
        f"{path}: --spine-concurrent-devices 6, --dataplane-concurrent-workers 2, "
        "--max-dataplane-tools 40, --publish (replaced on the command line)")


def test_run_configuration_says_when_no_profile_was_used(tmp_path):
    config = capture_run_configuration(SimpleNamespace(), _settings(state_dir=tmp_path), Budget(0, 0),
                                       skip_llm=True, dry_run=True)
    assert config["Profile"] == "None"
