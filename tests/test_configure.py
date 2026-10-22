"""nso-diagnostic-run --configure: questions that fill .env, and the startup check of required settings."""
import stat

import pytest
from dotenv import dotenv_values

EXAMPLE = """# NSO
NSO_ADDRESS=0.0.0.0
NSO_USERNAME=admin
NSO_PASSWORD=
# NSO_CA_BUNDLE=/path/to/ca-bundle.pem

# LLM
FABRIC_AI_API_KEY=
FABRIC_AI_API_URL=https://ai.fabric-testbed.net
FABRIC_AI_MODEL=your-claude-opus5
FABRIC_AI_TEMPERATURE=1
DRY_RUN=1
# SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...
# SMTP_HOST=smtp.example.com          # server hostname, not an email address
# SMTP_PORT=587
# PROMETHEUS_PUSHGATEWAY_URL=http://127.0.0.1:9091
SLACK_BOT_TOKEN=
SLACK_CHANNEL_ID=
"""


def _env(tmp_path, text=EXAMPLE):
    path = tmp_path / ".env"
    path.write_text(text)
    return path


def test_settings_are_written_in_place_keeping_comments(tmp_path):
    from agent.configure import update_env_file

    env = _env(tmp_path, "# NSO\nNSO_ADDRESS=0.0.0.0\nNSO_PASSWORD=\n# NSO_CA_BUNDLE=/path/to/ca.pem\n"
                         "# SMTP_HOST=smtp.example.com          # server hostname\nDRY_RUN=1\n")
    update_env_file(env, {"NSO_ADDRESS": "nso.example.org", "NSO_PASSWORD": 'p#ss "x" y',
                          "NSO_CA_BUNDLE": "/etc/ca.pem", "SMTP_HOST": "smtp.example.org",
                          "EMAIL_TO": "a@example.org,b@example.org"})

    assert env.read_text() == ('# NSO\nNSO_ADDRESS=nso.example.org\nNSO_PASSWORD="p#ss \\"x\\" y"\n'
                               'NSO_CA_BUNDLE=/etc/ca.pem\nSMTP_HOST=smtp.example.org\nDRY_RUN=1\n'
                               'EMAIL_TO=a@example.org,b@example.org\n')
    assert dotenv_values(env)["NSO_PASSWORD"] == 'p#ss "x" y'
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def _scripted(answers, secrets):
    """Answers in the order the questions come; records every prompt shown."""
    shown = []

    def ask(prompt):
        shown.append(prompt)
        return answers.pop(0)

    def ask_secret(prompt):
        shown.append(prompt)
        return secrets.pop(0)

    return ask, ask_secret, shown


def test_questions_fill_nso_and_the_sections_asked_for(tmp_path):
    from agent.configure import configure

    env = _env(tmp_path)
    ask, ask_secret, shown = _scripted(
        ["nso.example.org", "", "", "",      # address; username kept; verification kept on; no CA bundle
         "y", "", "gpt-x", "",                # LLM: URL and temperature kept
         "y", "b", "C0123ABCD",               # Slack through a bot
         "n", "n",                            # no email, no Pushgateway
         ""],                                 # run the connection check: yes
        ["s3cret", "sk-1", "xoxb-1"])
    checks = []

    code = configure(env, ask=ask, ask_secret=ask_secret, say=lambda *a: None, interactive=True,
                     run_check=lambda values: checks.append(values) or 0)

    values = dotenv_values(env)
    assert (values["NSO_ADDRESS"], values["NSO_USERNAME"], values["NSO_PASSWORD"]) == ("nso.example.org", "admin", "s3cret")
    assert (values["FABRIC_AI_API_KEY"], values["FABRIC_AI_MODEL"], values["FABRIC_AI_TEMPERATURE"]) == ("sk-1", "gpt-x", "1")
    assert (values["SLACK_BOT_TOKEN"], values["SLACK_CHANNEL_ID"]) == ("xoxb-1", "C0123ABCD")
    # Sections not asked for stay as they were.
    assert "SMTP_HOST" not in values and "PROMETHEUS_PUSHGATEWAY_URL" not in values
    assert "# SMTP_HOST=smtp.example.com" in env.read_text()
    # The example address is not offered as the current value, and no secret is ever shown.
    assert "0.0.0.0" not in shown[0]
    assert not any(s in p for p in shown for s in ("s3cret", "sk-1", "xoxb-1"))
    assert code == 0 and checks and checks[0]["NSO_PASSWORD"] == "s3cret"


def test_enter_everywhere_keeps_every_value(tmp_path):
    from agent.configure import configure

    configured = EXAMPLE.replace("NSO_ADDRESS=0.0.0.0", "NSO_ADDRESS=nso.example.org").replace(
        "NSO_PASSWORD=", "NSO_PASSWORD=s3cret").replace("FABRIC_AI_API_KEY=", "FABRIC_AI_API_KEY=sk-1")
    env = _env(tmp_path, configured)
    ask, ask_secret, shown = _scripted(["", "", "", "", "", "", "", "", "n", "n", "n", "n"], ["", ""])

    assert configure(env, ask=ask, ask_secret=ask_secret, say=lambda *a: None, interactive=True,
                     run_check=None) == 0
    assert env.read_text() == configured
    assert "[(set)]" in shown[2]          # the password prompt says it is set, not what it is


def test_skipping_certificate_verification_sets_nso_verify_0(tmp_path):
    from agent.configure import configure

    env = _env(tmp_path)
    ask, ask_secret, shown = _scripted(["nso.example.org", "", "y", "n", "n", "n", "n"], ["s3cret"])

    assert configure(env, ask=ask, ask_secret=ask_secret, say=lambda *a: None, interactive=True,
                     run_check=None) == 0
    assert dotenv_values(env)["NSO_VERIFY"] == "0"
    assert not any("CA bundle" in prompt for prompt in shown)    # not needed once checking is off


def test_verification_left_off_by_enter_and_turned_back_on_by_n(tmp_path):
    from agent.configure import configure

    env = _env(tmp_path, EXAMPLE.replace("NSO_PASSWORD=", "NSO_PASSWORD=s3cret\nNSO_VERIFY=0"))
    run = dict(say=lambda *a: None, interactive=True, run_check=None)

    ask, ask_secret, shown = _scripted(["nso.example.org", "", "", "n", "n", "n", "n"], [""])
    configure(env, ask=ask, ask_secret=ask_secret, **run)
    assert dotenv_values(env)["NSO_VERIFY"] == "0"
    assert any("verification" in prompt and "[Y/n]" in prompt for prompt in shown)

    ask, ask_secret, shown = _scripted(["", "", "n", "/etc/ca.pem", "n", "n", "n", "n"], [""])
    configure(env, ask=ask, ask_secret=ask_secret, **run)
    values = dotenv_values(env)
    assert (values["NSO_VERIFY"], values["NSO_CA_BUNDLE"]) == ("1", "/etc/ca.pem")


def test_a_pushgateway_answer_says_how_to_start_the_stack(tmp_path):
    from agent.configure import configure

    configured = EXAMPLE.replace("NSO_ADDRESS=0.0.0.0", "NSO_ADDRESS=nso.example.org").replace(
        "NSO_PASSWORD=", "NSO_PASSWORD=s3cret")
    (tmp_path / "deploy" / "monitoring").mkdir(parents=True)
    (tmp_path / "deploy" / "monitoring" / "README.md").write_text("")
    said = []
    ask, ask_secret, _ = _scripted(["", "", "", "", "n", "n", "n", "y", ""], [""])

    configure(_env(tmp_path, configured), ask=ask, ask_secret=ask_secret, say=said.append,
              interactive=True, run_check=None)
    assert any("deploy/monitoring/README.md" in line and "docker compose up -d" in line for line in said)


def test_without_a_terminal_it_only_reports_what_is_missing(tmp_path):
    from agent.configure import configure

    env = _env(tmp_path)
    said = []

    code = configure(env, ask=None, ask_secret=None, say=said.append, interactive=False, run_check=None)

    assert code == 2 and env.read_text() == EXAMPLE
    assert "NSO_ADDRESS is required (still the example value 0.0.0.0)" in said[0]
    assert "NSO_PASSWORD is required" in said[0]


def test_a_missing_env_is_created_from_the_example(tmp_path):
    from agent.configure import configure

    (tmp_path / ".env.example").write_text(EXAMPLE)
    code = configure(tmp_path / ".env", ask=None, ask_secret=None, say=lambda *a: None,
                     interactive=False, run_check=None)

    assert code == 2 and (tmp_path / ".env").read_text() == EXAMPLE


def test_startup_names_every_missing_setting_at_once(monkeypatch, capsys):
    from agent.config import SettingsError, load_settings
    from diagnostic_mas.run import main

    monkeypatch.setenv("NSO_ADDRESS", "0.0.0.0")
    monkeypatch.setenv("NSO_PASSWORD", "")
    monkeypatch.setenv("FABRIC_AI_API_KEY", "")
    with pytest.raises(SettingsError) as raised:
        load_settings()
    message = str(raised.value)
    for problem in ("NSO_ADDRESS is required (still the example value 0.0.0.0)",
                    "NSO_PASSWORD is required", "FABRIC_AI_API_KEY is required"):
        assert problem in message

    # The command says so in one message, without a traceback.
    assert main(["--check-connection"]) == 2
    err = capsys.readouterr().err
    assert "NSO_PASSWORD is required" in err and "nso-diagnostic-run --configure" in err
    assert "Traceback" not in err


def _linked_checkout(tmp_path):
    """macOS's layout: /tmp is a link to /private/tmp, and the checkout is under it."""
    real = tmp_path / "private" / "tmp" / "agent"
    real.mkdir(parents=True)
    (tmp_path / "tmp").symlink_to(tmp_path / "private" / "tmp")
    return real, tmp_path / "tmp" / "agent"


def test_paths_keep_the_folder_name_the_shell_shows(tmp_path):
    from agent.config import shell_path

    real, typed = _linked_checkout(tmp_path)
    assert shell_path(real / "state", pwd=str(typed), cwd=str(real)) == typed / "state"
    assert shell_path(real, pwd=str(typed), cwd=str(real)) == typed
    # A $PWD left over from another folder, a path outside it, or no $PWD: unchanged.
    assert shell_path(real / "state", pwd=str(tmp_path), cwd=str(real)) == real / "state"
    assert shell_path(tmp_path / "other", pwd=str(typed), cwd=str(real)) == tmp_path / "other"
    assert shell_path(real / "state", pwd=None, cwd=str(real)) == real / "state"


def test_configure_names_env_as_the_shell_does(tmp_path, monkeypatch):
    import agent.configure as configure_module

    real, typed = _linked_checkout(tmp_path)
    monkeypatch.chdir(real)
    monkeypatch.setenv("PWD", str(typed))
    seen = []
    monkeypatch.setattr(configure_module, "configure", lambda env_path, **kw: seen.append(env_path) or 0)

    configure_module.configure_from_cli()
    assert seen == [typed / ".env"]


def test_an_empty_username_means_the_default(monkeypatch):
    from agent.config import _mcp_args

    monkeypatch.setenv("NSO_ADDRESS", "nso.example.org")
    monkeypatch.setenv("NSO_USERNAME", "")
    assert "--nso-username=admin" in _mcp_args()
