# FAQ — NSO summary agent

Common questions about the Devices / topology sections of the ops summary.

## Topology basics

### What is a static edge vs an operational edge?

Two views of the **same** links (interfaces, IS-IS, BGP):

| | **Static** | **Operational** |
|---|------------|-----------------|
| Meaning | What **config says** should exist | What is **true right now** |
| Stored | `state/topology.static.json` (and copied into each snapshot as `topology.static`) | Inside each run snapshot only: `topology.operational` (not its own file) |
| Content | Edge inventory: `id`, `local` / `remote` | Live `state` keyed by the same `id` |
| Updated | When config / device inventory changes (or force rebuild). **Services** edges are also refreshed **every run** into the static file | Every `nso-summary-run` |

The Devices report **joins** them by edge `id`.

### Where do interface names like `HundredGigE0/0/0/9` come from?

From **NSO device config (CDB)** via `explore_nso_path` / `get_device_config` — the long YANG/config form — not from `show interfaces summary`.

Live status uses `show interfaces brief`, which prints short names (`Hu…`). The agent matches long ↔ short with deterministic prefix maps (`HundredGigE`↔`Hu`, `TwentyFiveGigE`↔`TF`, `BVI`↔`BV`, …).

---

## Counts that look “wrong”

### Why are fewer interfaces in the summary than on the device?

**Different inventories.**

| Source | What it counts |
|--------|----------------|
| Devices → `interfaces` | Interfaces present in **NSO config** (static physical edges) |
| `show interfaces brief` / `summary` ALL TYPES | **Every** interface the router knows (config + platform defaults + tunnels, etc.) |

Example on `uky-data-sw`: static had **43**; `show interfaces summary` reported **49**. Typical extras not in NSO config (or not discovered under the interface tree): `Null0`, `MgmtEth`, Bundle-Ether, SRTE tunnels, extra BVIs, subifs present only live, etc.

The summary intentionally tracks **configured** interfaces so it stays aligned with NSO intent.

### Why does BGP / IS-IS show 2 when `show` lists 3?

The Devices rollup counts **peers between NSO-managed devices in this topology graph**, not every CLI neighbor.

Live rows whose neighbor cannot be mapped to an NSO device are listed under **unmapped**, for example:

```text
  BGP peers: 2 (up 2)
    - unmapped (1): 10.133.0.1
  IS-IS adjacencies: 2 (up 2)
    - unmapped (1): star-data-sw
```

So `2` = fabric peers; `unmapped` explains the rest of what you see on the box.

### Why is `layers.services` edge count much smaller than service instance count?

**Edges are endpoint pairs, not instances.**

| Count | What it means |
|-------|----------------|
| `snapshot.services` / Service Summary | One row per **service instance** (health) |
| `topology.*.layers.services.summary.total` | One edge per **device pair** on an instance (or one local-only edge if a single device) |

Examples:

- One l2ptp with devices `lbnl-data-sw` + `renc-data-sw` → **1** edge (`svc:l2ptp:…:lbnl-data-sw:renc-data-sw`)
- One instance on three devices → **3** edges (all unique pairs)
- Single-device service (e.g. some bridges) → **1** edge with `remote: null` (id ends in `_local`)
- Instance with **no** devices extracted → **0** edges + low issue `service_no_devices`

Also only **in-scope** types/instances appear (same filters as collect: `IGNORE_SERVICE_TYPES`, `MAX_SERVICE_TYPES`). Types never returned by `get_service_types` (e.g. some fabnet*) never show up.

Operational `up` / `down` / … on the services layer count **edges**, so a 3-device `down` instance contributes **3** to `down`. Topology issues are still **one per instance** (`service_down`, etc.), keyed to the lex-smallest edge id.

Health tables and Devices service lines still use `snapshot.services` — unchanged.

### Why was an interface `unknown` (e.g. `FourHundredGigE0/0/0/34`)?

`unknown` means: in static config, but **not matched** to a live `interfaces brief` row (detail often `not in interfaces brief`).

Devices shows **NSO → box**:

- `→ Hu0/0/0/32  [suggested]` — heuristic (same port address / breakout); not admin-approved
- `→ (not on box)` — no live candidate (phantom / truly missing)
- After admin confirms in `INTERFACE_EQUIVALENCES_FILE`, the edge uses the box name for status and leaves the `unknown` group (`[confirmed]` only appears when a confirmation failed)

Common causes:

1. **Speed / breakout drift** — NSO still has `FourHundredGigE…/32` while the box shows `Hu…/32` or `Te…/36/0–3`.
2. **Stale / phantom config** — NSO lists a port the box doesn’t have.
3. **Collection error** — `exec_show interfaces brief` failed for that device.

Admin mappings live under `config/` by convention (any path works via env):

- `config/to_confirm.interface-equivalence.json` — draft of **suggested** matches (rewritten each run). When present, Devices opens with **Action required:** *N* unconfirmed … — review that file
- `config/interface-equivalences.json` — **confirmed** rows only; point `INTERFACE_EQUIVALENCES_FILE` here
- `config/interface-equivalences.example.json` — schema example

Do not set `INTERFACE_EQUIVALENCES_FILE` to the `to_confirm` draft until you have reviewed it (empty `box` rows are phantoms — fix NSO config, don’t confirm). After review, copy keepers into `interface-equivalences.json`.

---

## Status interpretation

### How is interface status determined?

1. Inventory = static interface names for that device.
2. Live = one `show interfaces brief` per device (Intf State + LineP State).
3. Devices reports a **single Status/Protocol pair** per interface:

| Label | Meaning |
|-------|---------|
| **up/up** | Admin up, protocol up (healthy) |
| **up/down** | Admin up, protocol down (link problem) |
| **down/down** | Admin down and protocol down |
| **admin-down** | Administratively shut |
| **unknown** | In NSO config, not found in brief |

Example:

```text
  interfaces: 43 (up/up 20, up/down 5, down/down 10, admin-down 7, unknown 1)
    - up/down (5)
        ...
    - down/down (10)
        ...
    - admin-down (7)
        ...
    - unknown (1)
        ...
```

Only non-`up/up` interfaces are listed under the rollup. (`down/up` is rare but would appear if seen.)

### How is BGP peer status determined?

From `show bgp summary` (or `show bgp ipv4 unicast summary`). On IOS-XR, an up peer’s `St/PfxRcd` column is often a **prefix count** (`0`, `445667`), not the word `Established`. The parser treats a numeric last field as Established (Idle/Active/… words still mean down).

`St/PfxRcd` = **State / Prefixes Received**: a number means up (+ how many prefixes received); a word means FSM state (not established).

### Why might BGP have shown `down` while peers looked up?

Before the numeric `St/PfxRcd` fix, established peers were parsed as `unknown`, and `unknown`+`unknown` was classified as **down**. Re-run after that fix so statuses refresh.

---

## Report layout

### What does `BGP peers: 2 (up 2)` mean?

The first number is the **total** of NSO-mapped sessions/adjacencies for that device. The parentheses are a **status breakdown of that same total** (the parts sum to the first number).

Examples:

| Line | Meaning |
|------|---------|
| `BGP peers: 2 (up 2)` | 2 peers total; both up |
| `BGP peers: 2 (up 1, down 1)` | 2 peers total; mixed status |
| `interfaces: 43 (up 20, down 15, admin-down 3, unknown 5)` | 43 configured interfaces; status split |

**Unmapped** live neighbors (outside the NSO inventory) are listed separately and are **not** included in that total.

### What does the Devices section show?

The report is two parts when `executive` is in `REPORT_SECTIONS` (default):

1. **Executive Summary** — Overall Status + Action Items (FABRIC), **Fleet Summary** (Python), Changes / Service Summary / Device Health, then **Operational Assessment** (FABRIC, one paragraph)  
2. **Detailed Device Analysis** — ASCII banner, then per-device Status + Observations (and optional ignored types):

CPU/memory alert thresholds for Fleet Summary → `config/fleet_summary_thresholds.yaml`.

Fleet Summary **Infrastructure** also lists Temperature / Fan / Power Supply / Control Plane Drop alerts from MCP `get_hardware_health` (any non-normal sensor, failed fan/PSU, or Σ control-plane drops > 0, counted per device). In Detailed Device Analysis, each device has a **Hardware** block; categories with no usable data show `Unavailable`.

```text
=====================================================
Detailed Device Analysis
=====================================================
```

Per device under that banner (sorted by name), a bannered device block with Status
(Overall + Reason), Routing, Routes, Services, Interfaces summary, and Exceptions
(inventory mismatch / operational down / unmapped peers / live-not-in-static —
admin-down stays in the Interfaces summary only). Live interfaces not in static
config appear under Exceptions only; they do **not** change Interface totals or
Inventory Review health.

Sections in the full report are controlled by `REPORT_SECTIONS` (see README). FABRIC AI writes **Problems / Failures** when that section is enabled, and optionally **Infrastructure Health** if `system_health` is listed (usually omit — Fleet Summary covers CPU/memory alerts). Omit `problems` / `system_health` to skip those LLM calls. CPU/memory is still collected when `executive` is enabled (for Fleet Summary). Hardware health (`get_hardware_health`) is collected when `executive` or `devices` is enabled.

### Is topology data in Prometheus?

**Phase 1+ gauges are pushed** when `PROMETHEUS_PUSHGATEWAY_URL` is set (see `deploy/monitoring/`). Each successful non-dry-run posts rollups such as fleet sync/in-sync/out-of-sync, services, ISIS/BGP/physical, topology issues, infra/hardware alerts, inventory review, delta counts, plus run success/duration/timestamp. Every series is labeled `pipeline="agent"` or `pipeline="multi-agent"`. Unset URL → skip. Pushgateway down → warning only; the report still completes.

Full topology graphs are **not** exported — only counts.

### How do I get a topology graph image?

Use Graphviz. From the repo root (install Graphviz first, e.g. `brew install graphviz`):

```bash
# Script writes DOT and renders
python3 scripts/export_topology_dot.py -o topo.dot --render png

# Same result in two steps
python3 scripts/export_topology_dot.py -o topo.dot
dot -Tpng topo.dot -o topo.png
```

Defaults to underlay + routing links. Add services pairs (and single-device self-loops):

```bash
python3 scripts/export_topology_dot.py -i state/topology.static.json \
  --layers underlay,routing,services -o topo.dot --render png
```

See [README — Graphviz export](../README.md#graphviz-export-scriptsexport_topology_dotpy).

---

## Related docs

- [NOTES-2026-07-13.md](NOTES-2026-07-13.md) — work done on 2026-07-13
- [NOTES-2026-07-28.md](NOTES-2026-07-28.md) — services topology layer (Phase 3)
- [agent/topology/docs/DESIGN.md](../agent/topology/docs/DESIGN.md) — static vs operational design
- [README.md](../README.md) — `REPORT_SECTIONS`, run instructions
