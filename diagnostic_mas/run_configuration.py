"""Credential-free snapshot of effective run options, captured before collection."""
from urllib.parse import urlsplit


def capture_run_configuration(args, settings, budget, *, skip_llm, dry_run):
    from agent.summarize import llm_timeout_seconds, llm_connect_timeout_seconds
    from diagnostic_mas.dataplane_verify import dataplane_tools_cap, _DATAPLANE_MAX_ROUNDS_CAP

    tools = dataplane_tools_cap(budget)
    def cap(name):
        value = getattr(args, name, None)
        return max(0, int(value)) if value is not None else None
    # Only extract numeric timeout arguments; never expose the command/environment.
    timeouts = {}
    argv = settings.mcp_server_args
    for index, arg in enumerate(argv):
        for flag in ("--nso-timeout", "--nso-exec-timeout"):
            value = (arg.split("=", 1)[1] if arg.startswith(flag + "=") else
                     argv[index + 1] if arg == flag and index + 1 < len(argv) else None)
            if value is not None:
                try:
                    timeouts[flag] = float(value)
                except ValueError:
                    pass
    try:
        host = urlsplit(settings.fabric_api_url).hostname or "Not recorded"
    except ValueError:
        host = "Not recorded"
    return {
        "Model": settings.fabric_model,
        "LLM temperature": settings.fabric_temperature if settings.fabric_temperature is not None else "Provider default (parameter omitted)",
        "LLM provider host": host,
        "LLM enabled": not skip_llm,
        "Device filter": getattr(args, "devices", None) or "All",
        "Service type filter": getattr(args, "service_type", None) or "All",
        "Service ID filter": getattr(args, "service_id", None) or "All",
        "Scope flags": ", ".join(name for name in ("isis_only", "bgp_only", "device_only", "service_only", "skip_service") if getattr(args, name, False)) or "Default",
        "Ignored service types": ", ".join(sorted(settings.ignore_service_types)) or "None",
        "Maximum service types": settings.max_service_types,
        "Dataplane services per category": cap("max_dataplane_per_category") if cap("max_dataplane_per_category") is not None else "Automatic: one per typed category; two if only one category",
        "Dataplane total service cap": cap("max_dataplane_services") if cap("max_dataplane_services") is not None else "No additional total cap",
        "Concurrent spine device calls": getattr(args, "spine_concurrent_devices", 1),
        "Concurrent dataplane workers": getattr(args, "dataplane_concurrent_works", 1),
        "Tools per dataplane dig": tools,
        "Rounds per dataplane dig": min(tools + 2, _DATAPLANE_MAX_ROUNDS_CAP),
        "Maximum drill issues": budget.max_drill_issues,
        "Tools per drill issue": budget.max_tools_per_drill,
        "Maximum port investigations": budget.max_port_investigations,
        "Maximum deep checks": budget.max_deep_checks,
        "Maximum handoffs": budget.max_handoffs,
        "Dataplane/drill LLM request timeout (seconds)": llm_timeout_seconds(settings),
        "LLM connection timeout (seconds)": llm_connect_timeout_seconds(settings),
        "LLM automatic retries": getattr(settings, "fabric_max_retries", 0),
        "NSO request timeout passed to MCP (seconds)": timeouts.get("--nso-timeout", "Server-controlled; not recorded"),
        "NSO live-command timeout passed to MCP (seconds)": timeouts.get("--nso-exec-timeout", "Server-controlled; not recorded"),
        "Service-sync mode": settings.service_sync_mode,
        "Dry run": dry_run,
        "Publish enabled": not dry_run,
        "Save raw MCP results": bool(getattr(args, "save_mcp_results", False)),
    }


def format_run_configuration(case):
    snapshot = getattr(case, "run_configuration", {})
    lines = ["## Run configuration", ""]
    if not snapshot:
        return lines + ["Not recorded for this run. Current settings are not substituted.", ""]
    for key, value in snapshot.items():
        if isinstance(value, bool):
            value = "Yes" if value else "No"
        value = str(value).replace("\n", " ").replace("\r", " ")
        lines.append(f"- **{key}:** {value}")
    return lines + [""]
