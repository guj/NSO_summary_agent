# Multi-Agent Topology Workflow — Human Operating SOP

**Status:** Draft  
**Scope:** `multi_agent/` / `nso-multi-agent-run` (sibling to `nso-summary-run`; separate state)  
**Audience:** Network operators / engineers who receive or run multi-agent reports  
**Automation boundary:** Read-only investigation and reporting. Humans approve any config or remediation change.

---

## 1. Purpose

Define what people do **after** a multi-agent run produces a report (stdout, Slack, email, or `state/multi_agent/runs/<run_id>/report.md` after `--publish`).

The agents discover bidirectional IS-IS/BGP issues, optional device evidence, and suggested Action Items.  
This SOP covers **who reviews**, **how to triage**, **when to escalate**, and **how to close**.

---

## 2. Roles

| Role | Responsibility |
|------|----------------|
| **Runner** | Starts the multi-agent job (manual or scheduled). Ensures `.env` / MCP / FABRIC AI are healthy. |
| **Primary reviewer** | Owns triage of Action Items within the review window. |
| **Escalation contact** | Network SME / on-call for persistent or high-impact routing faults. |
| **Change owner** | Opens/implements any remediation under normal change process (outside this agent). |

Fill names/rotas for your lab or production pocket:

| Role | Name / rota |
|------|-------------|
| Primary reviewer | jgu@lbl.gov |
| Escalation contact | jgu@lbl.gov |
| Change process | _TBD (ticket system / CAB)_ |

---

## 3. Triggers

| Trigger | Expected human action |
|---------|------------------------|
| Manual run (`nso-multi-agent-run …`) | Runner reviews stdout (and artifacts under `state/multi_agent/runs/` if `--publish`) before leaving the session. |
| Published run (`--publish`) | Primary reviewer reads Slack/email within **1 business hour**. |
| Scheduled run (if you add cron later) | Same as published; treat as scheduled health check. |
| Ad-hoc incident (“BGP looks wrong”) | Runner executes targeted run (`--devices …` or full), then Primary reviewer triages. |

**Dry-run / spine-only:** Still review findings; do not expect Slack/email unless `--publish` was used.

---

## 4. How to read a report (5 minutes)

Work top-down:

1. **Overall Status** — red/healthy signal for IS-IS, BGP, devices.  
2. **Fleet Summary** — sync, services, peer/adjacency counts, infra alerts.  
3. **Action Items** — ordered work queue (start here for triage).  
4. **Operational Assessment** — narrative context; verify against issue codes below.  
   When LLM ran (not `--spine-only`), also read **Suggested remedies (hypotheses)** —
   treat as unproven triage hints grounded in issues/evidence, not approved changes.  
5. **Detailed Analysis** — IS-IS / BGP sections + per-device evidence.  
6. **Artifacts** (if needed) — `state/multi_agent/runs/<run_id>/{isis,bgp,merged,report}.*` after `--publish` for edge ids and raw facts.

**Trust rules**

- Prefer **issue codes + edge ids** over free-text narrative.  
- Topology spine is authoritative for up/down/unidirectional; LLM text must not override it.  
- `collection_error` means **data was incomplete** — do not conclude “network is fine” or “peer is down” from that alone.

---

## 5. Severity & response time

Use issue **code + operational impact**, not only LLM wording.

| Priority | Criteria | Review / act within |
|----------|----------|---------------------|
| **P1** | Bidirectional IS-IS `down` or `unidirectional_adjacency` on production path; BGP `configured_no_session` / established count collapse affecting traffic | **30 minutes** acknowledge; escalate if not understood in 1 hour |
| **P2** | `missing_reverse_session`, sustained Idle/Active peers, hardware alerts with topology impact | **4 business hours** |
| **P3** | `unknown_neighbor_address`, `unknown_neighbor_system_id`, inventory mapping gaps, one-off collection timeouts | **1 business day** |
| **P4** | Informational / no issues (“no bidirectional issues detected”) | No action; archive awareness |

If **Overall Status is red** and Action Items are empty, treat as **P2** and inspect Detailed Analysis + artifacts.

---

## 6. Response playbooks (by issue code)

For each Action Item: identify code → follow playbook → record outcome (ticket or notes).

### 6.1 `collection_error` / `hardware_collect_error` / `system_health_collect_error`

**Meaning:** Agent could not reach NSO/device path (timeout, HTTPS error, MCP failure).  
**Human steps:**

1. Confirm NSO and MCP reachability (`nso-summary-run --list-tools` or re-run multi-agent).  
2. Retry once:  
   `nso-multi-agent-run --devices <device> --spine-only`  
3. If still failing, check device mgmt path / NSO device southbound — **do not** chase routing bugs yet.  
4. Escalate to platform/NSO owner if retries fail for **2 consecutive runs**.

### 6.2 `unidirectional_adjacency` (IS-IS)

**Meaning:** One side sees the neighbor; reverse side does not (or not Up).  
**Human steps:**

1. Note both devices + interfaces from `edge_id` / message.  
2. On each box (via NSO / approved CLI path): verify IS-IS enabled, interface up, area/level, auth, MTU.  
3. Compare with report evidence (`exec_show` results under Devices if present).  
4. If confirmed real: open change/ticket; do **not** use this agent to push config.  
5. After fix: re-run multi-agent and confirm adjacency `up` both ways.

### 6.3 `configured_no_session` (BGP)

**Meaning:** Static/config expects a session; live state is not Established.  
**Human steps:**

1. Capture peer IPs and devices from Action Item / edge id.  
2. Verify reachability of peer address (routing/IGP first if underlay unhealthy).  
3. Check BGP config both sides (ASN, update-source, timers, filters).  
4. Classify: underlay vs BGP config vs address mapping.  
5. Ticket + change as needed; re-run to confirm Established both ways.

### 6.4 `missing_reverse_session` / `unpaired_bgp_config`

**Meaning:** One device reports a peer row the other does not reciprocate (or config unpaired).  
**Human steps:**

1. Confirm whether peer should exist (admin intent / design).  
2. If yes: missing config or wrong neighbor IP on reverse device.  
3. If no: stale/extra neighbor — clean up under change control.  
4. Watch for pairing with `collection_error` on the far end (may be false asymmetry).

### 6.5 `unknown_neighbor_address` / `unknown_neighbor_system_id`

**Meaning:** Live neighbor not mapped to an NSO device / inventory identity.  
**Human steps:**

1. Decide if neighbor is expected (external peer, new device, lab box).  
2. Update inventory / naming / static topology mapping as appropriate.  
3. Do not treat as a down adjacency unless operational counts also show down/uni.

### 6.6 Hardware / infrastructure alerts (Device section)

**Meaning:** Fans, power, temperature, control-plane drops, or CPU/memory alerts on issue devices.  
**Human steps:**

1. Open the device block in Detailed Analysis.  
2. Corroborate with evidence; open hardware ticket if sensor/PSU/fan failed.  
3. If only collect errors, follow §6.1.

---

## 7. Standard triage checklist

Copy into ticket or shift notes:

```text
[ ] Run id / timestamp: ________
[ ] Trigger: manual / publish / incident
[ ] Overall Status: ________
[ ] P1/P2 items listed: ________
[ ] Collection errors present? Y/N — if Y, fix reachability first
[ ] IS-IS: up/down/uni counts: ________
[ ] BGP: established/down counts: ________
[ ] Action taken: monitor / ticket / escalate / change
[ ] Ticket ID: ________
[ ] Re-run clean? Y/N / scheduled
```

---

## 8. Escalation

Escalate to **Escalation contact** when any of these hold:

- P1 not understood or not mitigated within **1 hour**  
- Same P1/P2 issue code + edge id on **2 consecutive runs**  
- Multiple devices red with traffic impact suspected  
- Agent repeatedly cannot collect (MCP/NSO outage) blocking visibility  

Escalation package (minimum):

1. `state/multi_agent/runs/<run_id>/report.md` after publish (or Slack/email / stdout)  
2. Top Action Items with issue codes  
3. What you already checked  
4. Whether this is new vs unchanged since prior run  

---

## 9. Completion criteria (“done”)

A finding is **closed** when:

1. Root cause classified (real fault vs mapping/collection), and  
2. Either:  
   - **Monitor** — accepted risk / external expected peer documented, or  
   - **Ticket/change** filed with owner, or  
   - **Fixed** and a follow-up multi-agent run no longer lists that edge/code as active  

Do not close solely because the LLM narrative sounds optimistic.

---

## 10. Safe use of the agent

| Allowed | Not allowed |
|---------|-------------|
| Investigate with multi-agent / MCP read tools | Push config or `sync-to` from this workflow |
| Re-run with `--spine-only`, `--devices`, `--skip-llm` | Treat LLM plan/deep-checks as authoritative over spine |
| Publish reports to Slack/email | Skip human review on red Overall Status |
| Attach artifacts to tickets | Assume production `state/` was updated (it is not) |

Quick commands:

```bash
cd /path/to/NSO_summary_agent
source .venv/bin/activate
set -a && source .env && set +a

# Fast deterministic check
nso-multi-agent-run --spine-only

# Focus devices from Action Items
nso-multi-agent-run --devices lbnl-data-sw,renc-data-sw

# Notify humans + persist state/multi_agent/
nso-multi-agent-run --publish
```

---

## 11. Relationship to production summary agent

| | `nso-summary-run` | `nso-multi-agent-run` |
|--|-------------------|------------------------|
| Role | Scheduled fleet reporter | Topology/device investigation workflow |
| Updates `state/` | `state/latest.json` | `state/multi_agent/latest.json` (on deliver only) |
| Human SOP | Separate (fleet report ops) | **This document** |

If both fire, triage **P1 routing issues from multi-agent first**, then fleet/service deltas from the summary agent.

---

## 12. Open items for your org

Replace `_TBD_` in §2, then decide:

1. Who receives `--publish` Slack/email?  
2. Is multi-agent on a schedule, or on-demand only?  
3. Ticket system + severity mapping (P1–P4 → your labels)?  
4. Lab vs production: which devices are in scope for P1?

---

## Document control

| Field | Value |
|-------|-------|
| Owner | jgu@lbl.gov |
| Review cadence | Quarterly or after major agent/MCP change |
| Related code | `multi_agent/` |
| Related design | `docs/superpowers/specs/2026-08-10-multi-agent-production-mvp-design.md` |
