"""nso-diagnostic-run --configure: ask for the settings a scan needs and write them into .env.

Press Enter to keep the value shown in brackets. Secrets are read without echo and
shown only as "(set)". A section answered "no" is left exactly as it was.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from agent.config import EXAMPLE_VALUES, _parse_bool_env, missing_settings, shell_path


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    secret: bool = False
    default: str = ""


NSO = (
    Field("NSO_ADDRESS", "NSO hostname or IP, one that its HTTPS certificate names"),
    Field("NSO_USERNAME", "NSO username", default="admin"),
    Field("NSO_PASSWORD", "NSO password", secret=True),
)
NSO_CA = (Field("NSO_CA_BUNDLE", "CA bundle path, if NSO's certificate is from a private CA (Enter to skip)"),)
LLM = (
    Field("FABRIC_AI_API_URL", "LLM base URL (OpenAI-compatible)", default="https://ai.fabric-testbed.net"),
    Field("FABRIC_AI_API_KEY", "LLM API key", secret=True),
    Field("FABRIC_AI_MODEL", "LLM model id"),
    Field("FABRIC_AI_TEMPERATURE", "Temperature (Enter for the default; some models need 1)"),
)
SLACK_BOT = (
    Field("SLACK_BOT_TOKEN", "Slack bot token (xoxb-…, with the files:write scope)", secret=True),
    Field("SLACK_CHANNEL_ID", "Slack channel ID, such as C0123ABCD (not the channel name)"),
)
SLACK_WEBHOOK = (Field("SLACK_WEBHOOK_URL", "Slack incoming webhook URL", secret=True),)
EMAIL = (
    Field("SMTP_HOST", "SMTP server hostname (not an email address)"),
    Field("SMTP_PORT", "SMTP port", default="587"),
    Field("SMTP_USER", "SMTP login (Enter if the server needs none)"),
    Field("SMTP_PASSWORD", "SMTP password (Enter if none)", secret=True),
    Field("EMAIL_FROM", "From address"),
    Field("EMAIL_TO", "To addresses, separated by commas or semicolons"),
)
PUSHGATEWAY = (Field("PROMETHEUS_PUSHGATEWAY_URL", "Pushgateway URL", default="http://127.0.0.1:9091"),)

_SETTING = re.compile(r"^\s*(#\s*)?([A-Z][A-Z0-9_]*)=")


def _quote(value: str) -> str:
    """A .env value that python-dotenv reads back unchanged."""
    if re.fullmatch(r"[A-Za-z0-9_./:@,+=%~-]*", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def update_env_file(path: Path, updates: Mapping[str, str]) -> None:
    """Set each key in place: its active line, else its commented-out line, else a new line at the end."""
    lines = path.read_text().splitlines() if path.exists() else []
    pending = dict(updates)
    for commented in (False, True):
        for i, line in enumerate(lines):
            match = _SETTING.match(line)
            if match and match[2] in pending and bool(match[1]) == commented:
                lines[i] = f"{match[2]}={_quote(pending.pop(match[2]))}"
    lines += [f"{key}={_quote(value)}" for key, value in pending.items()]
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def _current(values: Mapping[str, str], key: str) -> str:
    value = values.get(key) or ""
    return "" if value in EXAMPLE_VALUES.get(key, ()) else value


def _ask_fields(fields, values, updates, ask, ask_secret) -> None:
    for field in fields:
        value = _current(values, field.key)
        shown = "(set)" if field.secret and value else (value or field.default)
        prompt = f"  {field.label}" + (f" [{shown}]" if shown else "") + ": "
        answer = (ask_secret if field.secret else ask)(prompt).strip()
        if answer:
            updates[field.key] = answer
        elif not value and field.default:
            updates[field.key] = field.default


def _yes(ask, question: str, default: bool) -> bool:
    answer = ask(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not answer else answer.startswith("y")


def configure(env_path: Path, *, ask: Callable[[str], str] | None, ask_secret: Callable[[str], str] | None,
              say: Callable[[str], object], interactive: bool,
              run_check: Callable[[dict[str, str]], int] | None) -> int:
    """Ask for the settings and write them; 0 when the required ones are set, else 2."""
    if not env_path.exists():
        example = env_path.with_name(".env.example")
        if not example.exists():
            say(f"No {env_path.name} or .env.example in {env_path.parent}: run this from the checkout root.")
            return 2
        shutil.copyfile(example, env_path)
        os.chmod(env_path, 0o600)
    values = {key: value or "" for key, value in dotenv_values(env_path).items()}

    if not interactive:
        missing = missing_settings(values, require_llm_key=False)
        say(("Settings still needed in .env: " + "; ".join(missing) + ".") if missing
            else "The required settings are set.")
        return 2 if missing else 0

    updates: dict[str, str] = {}
    say("NSO (required). Press Enter to keep the value in brackets.")
    _ask_fields(NSO, values, updates, ask, ask_secret)
    skipped = not _parse_bool_env(values.get("NSO_VERIFY"), default=True)
    if _yes(ask, "  Skip NSO certificate verification (NSO_VERIFY=0), as for a self-signed certificate?", skipped):
        updates["NSO_VERIFY"] = "0"
    else:
        if skipped:
            updates["NSO_VERIFY"] = "1"
        _ask_fields(NSO_CA, values, updates, ask, ask_secret)
    if _yes(ask, "Set up an LLM for investigations? (without one, scans run with --skip-llm)",
            bool(values.get("FABRIC_AI_API_KEY"))):
        _ask_fields(LLM, values, updates, ask, ask_secret)
    if _yes(ask, "Send reports to Slack?", bool(values.get("SLACK_BOT_TOKEN") or values.get("SLACK_WEBHOOK_URL"))):
        webhook_only = bool(values.get("SLACK_WEBHOOK_URL")) and not values.get("SLACK_BOT_TOKEN")
        answer = ask(f"  Upload the HTML report with a bot (b), or post a one-line notice with a webhook (w)? "
                     f"[{'w' if webhook_only else 'b'}] ").strip().lower() or ("w" if webhook_only else "b")
        if answer.startswith("w"):
            _ask_fields(SLACK_WEBHOOK, values, updates, ask, ask_secret)
        else:
            _ask_fields(SLACK_BOT, values, updates, ask, ask_secret)
            say("  Invite the bot to that channel in Slack: /invite @your-app-name")
    if _yes(ask, "Send reports by email?", bool(values.get("EMAIL_TO"))):
        _ask_fields(EMAIL, values, updates, ask, ask_secret)
    if _yes(ask, "Push counts to a Prometheus Pushgateway, for Grafana?",
            bool(values.get("PROMETHEUS_PUSHGATEWAY_URL"))):
        _ask_fields(PUSHGATEWAY, values, updates, ask, ask_secret)
        if (env_path.parent / "deploy" / "monitoring" / "README.md").exists():
            say("  This only sends the counts. Start Pushgateway, Prometheus and Grafana yourself: "
                "docker compose up -d in deploy/monitoring, as in deploy/monitoring/README.md.")

    changed = {key: value for key, value in updates.items() if values.get(key) != value}
    if changed:
        update_env_file(env_path, changed)
        say(f"Saved {len(changed)} setting(s) to {env_path}.")
    else:
        say("No changes.")
    values.update(changed)
    missing = missing_settings(values, require_llm_key=False)
    if missing:
        say("Still needed: " + "; ".join(missing) + ". Run --configure again to set them.")
        return 2
    if run_check and _yes(ask, "Run the connection check now?", True):
        return run_check(values)
    return 0


def configure_from_cli() -> int:
    """The --configure option: edit .env in the current directory."""

    def run_check(values: dict[str, str]) -> int:
        # A fresh process, given the new values: .env was read before they were written.
        env = {**os.environ, **values}
        return subprocess.run([sys.executable, "-m", "diagnostic_mas.run", "--check-connection"], env=env).returncode

    return configure(shell_path(Path.cwd()) / ".env", ask=input, ask_secret=getpass.getpass, say=print,
                     interactive=sys.stdin.isatty(), run_check=run_check)
