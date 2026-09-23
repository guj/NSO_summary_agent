---
description: Device-focused diagnostic (sync/HW only); requires device name
argument-hint: "[llm] device|device1,device2"
---

Run a **device-focused** diagnostic MAS pass. Do **not** publish.

1. Parse `$ARGUMENTS` (whitespace-separated):
   - Token `llm` (case-insensitive) → enable LLM (omit `--skip-llm`). Default: `--skip-llm`.
   - Remaining tokens → device filter (**required**). Prefer one comma-list
     (`renc-data-sw,lbnl-data-sw`) or a substring (`renc`).
     Pass as `--devices …`. If no device token, stop and ask for a device name.
2. Confirm repo root + `.env`; activate `.venv` if present; then
   `set -a && source .env && set +a`.
3. Run (dry-run):

```bash
nso-diagnostic-run --device-only --skip-llm --dry-run --devices DEVICES

# with llm (drop --skip-llm)
nso-diagnostic-run --device-only --dry-run --devices DEVICES
```

   **Lean MCP** (device info only):
   - `list_devices` (to resolve substring → full name)
   - `get_fleet_sync_summary` (sync column)
   - `get_hardware_health` / system health for those devices
   - Does **not** walk service types, get_services, ISIS, BGP, or physical inventory

4. Summarize from stdout (operator **Devices** sections):
   - Sync / Health / Attention for named devices
   - If `llm`: Summary bullets when present
5. Ground every claim in CLI output. No inventing. No `--publish` / `DRY_RUN=0`.
6. End noting dry-run + LLM on/off; suggest `/nso-service`, `/nso-bgp`, `/nso-isis`,
   `/nso-report` for broader checks.
