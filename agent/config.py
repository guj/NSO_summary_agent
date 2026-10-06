"""Environment configuration."""

from __future__ import annotations

import os
import math
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


DEFAULT_IGNORE_SERVICE_TYPES: tuple[str, ...] = ("idipa",)
DEFAULT_MAX_SERVICE_TYPES: int = 10
DEFAULT_REPORT_SECTIONS: tuple[str, ...] = (
    "executive",
    "devices",
    "ignored_types",
)
_VALID_REPORT_SECTIONS = frozenset(
    {
        "executive",
        "problems",
        "counts",
        "delta",
        "fleet_sync",
        "system_health",
        "devices",
        "ignored_types",
    }
)
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


@dataclass(frozen=True)
class Settings:
    mcp_server_cmd: str
    mcp_server_args: list[str]
    mcp_env: dict[str, str]
    fabric_api_key: str
    fabric_api_url: str
    fabric_model: str
    state_dir: Path
    dry_run: bool
    ignore_service_types: frozenset[str]
    max_service_types: int
    report_sections: tuple[str, ...]
    slack_webhook_url: str | None
    smtp_host: str | None 
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    smtp_use_tls: bool
    email_from: str | None
    email_to: list[str]
    email_subject_prefix: str
    prometheus_pushgateway_url: str | None
    prometheus_job: str
    prometheus_instance: str
    prompts_dir: Path
    topology_force_update: bool
    interface_equivalences_file: Path | None
    iface_troubleshoot_max_tool_rounds: int
    iface_troubleshoot_disable: bool
    slack_bot_token: str | None = None
    slack_channel_id: str | None = None
    diagnostic_report_base_url: str | None = None
    service_sync_mode: str = "check"
    fabric_temperature: float | None = 0.1
    fabric_max_retries: int = 0
    fabric_chat_timeout_sec: float = 60.0
    fabric_chat_connect_timeout_sec: float = 20.0


def _parse_temperature(raw: str | None) -> float | None:
    if raw is None:
        return 0.1
    if not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("FABRIC_AI_TEMPERATURE must be blank or a number between 0 and 2") from None
    if not math.isfinite(value) or not 0 <= value <= 2:
        raise ValueError("FABRIC_AI_TEMPERATURE must be blank or a number between 0 and 2")
    return value


def llm_temperature_kwargs(settings: Settings) -> dict[str, float]:
    """Omit the API parameter entirely when the provider default is requested."""
    value = getattr(settings, "fabric_temperature", 0.1)
    return {} if value is None else {"temperature": value}


def _mcp_env(nso_address: str, nso_password: str) -> dict[str, str]:
    env = {**os.environ, "NSO_PASSWORD": nso_password}
    no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or ""
    hosts = [part.strip() for part in no_proxy.split(",") if part.strip()]
    if nso_address not in hosts:
        hosts.append(nso_address)
    joined = ",".join(hosts)
    env["NO_PROXY"] = joined
    env["no_proxy"] = joined
    return env


def _parse_bool_env(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    text = value.strip().lower()
    if not text:
        return default
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _mcp_args() -> list[str]:
    args = [
        f"--nso-scheme={os.environ.get('NSO_SCHEME', 'https')}",
        f"--nso-address={os.environ['NSO_ADDRESS']}",
        f"--nso-port={os.environ.get('NSO_PORT', '443')}",
        f"--nso-username={os.environ.get('NSO_USERNAME', 'admin')}",
        f"--nso-timeout={os.environ.get('NSO_TIMEOUT', '10')}",
    ]
    # MCP default is verify=True; pass an explicit flag either way.
    if _parse_bool_env(os.environ.get("NSO_VERIFY"), default=True):
        args.append("--nso-verify")
    else:
        args.append("--no-nso-verify")
    ca_bundle = (os.environ.get("NSO_CA_BUNDLE") or "").strip()
    if ca_bundle:
        args.append(f"--nso-ca-bundle={ca_bundle}")
    return args


def _parse_chat_timeout(raw: str | None) -> float:
    if raw is None:
        return 60.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("FABRIC_CHAT_TIMEOUT_SEC must be a positive finite number") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError("FABRIC_CHAT_TIMEOUT_SEC must be a positive finite number")
    return value


def _parse_connect_timeout(raw: str | None) -> float:
    if raw is None:
        return 20.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("FABRIC_CHAT_CONNECT_TIMEOUT_SEC must be a positive finite number") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError("FABRIC_CHAT_CONNECT_TIMEOUT_SEC must be a positive finite number")
    return value


def _parse_max_retries(raw: str | None) -> int:
    if raw is None:
        return 0
    try:
        value = int(raw)
    except (ValueError, TypeError):
        raise ValueError("FABRIC_CHAT_MAX_RETRIES must be a nonnegative integer") from None
    if value < 0:
        raise ValueError("FABRIC_CHAT_MAX_RETRIES must be a nonnegative integer")
    return value


def load_settings(*, require_llm_key: bool = True) -> Settings:
    nso_password = os.environ.get("NSO_PASSWORD")
    if not nso_password:
        raise RuntimeError("NSO_PASSWORD is required")

    fabric_key = os.environ.get("FABRIC_AI_API_KEY") or ""
    if require_llm_key and not fabric_key:
        raise RuntimeError("FABRIC_AI_API_KEY is required")

    nso_address = os.environ.get("NSO_ADDRESS")
    if not nso_address:
        raise RuntimeError("NSO_ADDRESS is required")

    root = Path(__file__).resolve().parent.parent
    mcp_cmd = os.environ.get(
        "MCP_SERVER_CMD",
        "cisco-nso-mcp-server",
    )

    return Settings(
        mcp_server_cmd=mcp_cmd,
        mcp_server_args=_mcp_args(),
        mcp_env=_mcp_env(nso_address, nso_password),
        fabric_api_key=fabric_key,
        fabric_api_url=os.environ.get("FABRIC_AI_API_URL", "https://ai.fabric-testbed.net"),
        fabric_model=os.environ.get("FABRIC_AI_MODEL", "gpt-oss-20b"),
        fabric_temperature=_parse_temperature(os.environ.get("FABRIC_AI_TEMPERATURE")),
        fabric_max_retries=_parse_max_retries(os.environ.get("FABRIC_CHAT_MAX_RETRIES")),
        fabric_chat_timeout_sec=_parse_chat_timeout(os.environ.get("FABRIC_CHAT_TIMEOUT_SEC")),
        fabric_chat_connect_timeout_sec=_parse_connect_timeout(os.environ.get("FABRIC_CHAT_CONNECT_TIMEOUT_SEC")),
        state_dir=Path(os.environ.get("STATE_DIR", root / "state")),
        # Default dry-run (safe): no state write / Slack / email unless DRY_RUN=0 or --publish
        dry_run=os.environ.get("DRY_RUN", "1") in ("1", "true", "yes"),
        ignore_service_types=_parse_ignore_service_types(
            os.environ.get("IGNORE_SERVICE_TYPES")
        ),
        max_service_types=max(
            1,
            int(
                os.environ.get(
                    "MAX_SERVICE_TYPES", str(DEFAULT_MAX_SERVICE_TYPES)
                )
            ),
        ),
        report_sections=_parse_report_sections(os.environ.get("REPORT_SECTIONS")),
        slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL"),
        slack_bot_token=os.environ.get("SLACK_BOT_TOKEN") or None,
        slack_channel_id=os.environ.get("SLACK_CHANNEL_ID") or None,
        diagnostic_report_base_url=os.environ.get("DIAGNOSTIC_REPORT_BASE_URL") or None,
        smtp_host=os.environ.get("SMTP_HOST"),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_user=os.environ.get("SMTP_USER"),
        smtp_password=os.environ.get("SMTP_PASSWORD"),
        smtp_use_tls=os.environ.get("SMTP_USE_TLS", "1") in ("1", "true", "yes"),
        email_from=os.environ.get("EMAIL_FROM"),
        email_to=_parse_email_list(os.environ.get("EMAIL_TO", "")),
        email_subject_prefix=os.environ.get("EMAIL_SUBJECT_PREFIX", "NSO Summary"),
        prometheus_pushgateway_url=os.environ.get("PROMETHEUS_PUSHGATEWAY_URL") or None,
        prometheus_job=os.environ.get("PROMETHEUS_JOB", "nso-summary"),
        prometheus_instance=os.environ.get("PROMETHEUS_INSTANCE", "default"),
        prompts_dir=root / "prompts",
        topology_force_update=os.environ.get("TOPOLOGY_FORCE_UPDATE", "0")
        in ("1", "true", "yes"),
        interface_equivalences_file=_parse_optional_path(
            os.environ.get("INTERFACE_EQUIVALENCES_FILE")
        ),
        iface_troubleshoot_max_tool_rounds=max(
            1, int(os.environ.get("IFACE_TROUBLESHOOT_MAX_TOOL_ROUNDS", "10"))
        ),
        iface_troubleshoot_disable=os.environ.get(
            "IFACE_TROUBLESHOOT_DISABLE", "0"
        )
        in ("1", "true", "yes"),
        service_sync_mode=_parse_service_sync_mode(
            os.environ.get("NSO_SERVICE_SYNC_MODE")
        ),
    )


def _parse_service_sync_mode(value: str | None) -> str:
    from nso_facts.service_collect import normalize_service_sync_mode

    return normalize_service_sync_mode(value)


def _parse_optional_path(value: str | None) -> Path | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return Path(text).expanduser()


def _parse_email_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_ignore_service_types(value: str | None) -> frozenset[str]:
    """Return normalized service types to omit from collection and reports."""
    if value is None:
        items = list(DEFAULT_IGNORE_SERVICE_TYPES)
    else:
        items = [part.strip() for part in value.split(",") if part.strip()]
    return frozenset(item.lower() for item in items)


def _parse_report_sections(value: str | None) -> tuple[str, ...]:
    """Return ordered report section names from REPORT_SECTIONS."""
    import sys

    if value is None:
        return DEFAULT_REPORT_SECTIONS
    raw = [part.strip().lower() for part in value.split(",")]
    raw = [part for part in raw if part]
    if not raw:
        print(
            "warning: REPORT_SECTIONS is empty; using default",
            file=sys.stderr,
        )
        return DEFAULT_REPORT_SECTIONS

    out: list[str] = []
    unknown: list[str] = []
    for name in raw:
        if name in _VALID_REPORT_SECTIONS:
            out.append(name)
        else:
            unknown.append(name)
    for name in unknown:
        print(
            f"warning: unknown REPORT_SECTIONS entry ignored: {name}",
            file=sys.stderr,
        )
    if not out:
        print(
            "warning: REPORT_SECTIONS had no valid entries; using default",
            file=sys.stderr,
        )
        return DEFAULT_REPORT_SECTIONS
    return tuple(out)


def email_configured(settings: Settings) -> bool:
    return bool(settings.email_to)


def resolve_dry_run(
    *,
    settings: Settings,
    publish: bool = False,
    dry_run_flag: bool = False,
) -> bool:
    """Unify nso-summary-run and multi-agent delivery gating.

    - ``--publish`` → deliver (dry_run False)
    - ``--dry-run`` → dry (no state / Slack / email)
    - else → ``settings.dry_run`` (env ``DRY_RUN``, default True / ``1``)
    """
    if publish and dry_run_flag:
        raise ValueError("Use either --publish or --dry-run, not both")
    if publish:
        return False
    if dry_run_flag:
        return True
    return settings.dry_run


def validate_email_settings(settings: Settings) -> list[str]:
    """Validate email delivery settings. Returns warnings; raises ValueError on errors."""
    if not email_configured(settings):
        return []

    errors: list[str] = []
    warnings: list[str] = []

    if not settings.smtp_host:
        errors.append("SMTP_HOST is required when EMAIL_TO is set")
    if not settings.email_from:
        errors.append("EMAIL_FROM is required when EMAIL_TO is set")
    if not settings.email_to:
        errors.append("EMAIL_TO must list at least one recipient")

    if settings.smtp_port < 1 or settings.smtp_port > 65535:
        errors.append(f"SMTP_PORT must be between 1 and 65535 (got {settings.smtp_port})")

    if settings.email_from and not _looks_like_email(settings.email_from):
        errors.append(f"EMAIL_FROM is not a valid email address: {settings.email_from!r}")

    for address in settings.email_to:
        if not _looks_like_email(address):
            errors.append(f"EMAIL_TO contains invalid address: {address!r}")

    if settings.smtp_password and not settings.smtp_user:
        errors.append("SMTP_PASSWORD is set but SMTP_USER is missing")
    if settings.smtp_user and not settings.smtp_password:
        warnings.append(
            "SMTP_USER is set without SMTP_PASSWORD — auth may fail unless the relay allows it"
        )

    if errors:
        raise ValueError("Invalid email configuration:\n- " + "\n- ".join(errors))

    return warnings


def _looks_like_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value.strip()))
