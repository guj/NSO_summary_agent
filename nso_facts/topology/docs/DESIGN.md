# Topology snapshot design

## Goals

1. **Discover** connectivity and control-plane relationships across NSO-managed devices.
2. **Validate** bidirectional state where possible (A sees B and B sees A).
3. **Persist** the **static** topology under `state/` and rebuild it only when device/service **config** changes or a **force-update** flag is set.
4. **Refresh** the **operational** topology every run from live NSO/device state (adjacency up/down, sync, session state).
5. **Attach** both views to each run snapshot for reports and delta.
6. **Report** deterministic layer sections in markdown / Slack / email; optional LLM narrative on `issues` only.
7. **Delta** operational changes run-over-run; static changes when config triggers a rebuild.

Topology collection is **deterministic** (Python + MCP). The LLM does not invent adjacencies or peerings.

## Static vs operational

Two versions of the same layered model (`physical`, `underlay`, `routing`, `services`):

| Version | Meaning | Source | When updated |
|---------|---------|--------|--------------|
| **Static** | What **config says** should exist — devices, interfaces, IS-IS adjacencies, BGP peers, service endpoints as **configured** in NSO CDB / device intent | Config-oriented MCP (`explore_nso_path`, device/service config, compare tools) | Only when config changes: new/removed device, interface added/removed, neighbor/peer/service endpoint change, or **force update** |
| **Operational** | What is **true right now** — oper/admin state, established adjacencies, BGP sessions, service health, device sync | Live MCP (`check_isis_adjacencies`, `verify_bgp_peer_reachability`, `get_interface_health`, fleet sync, …) | **Every** summary run |

**Static** answers: “What is supposed to be connected?”  
**Operational** answers: “Is it up, synced, and working as configured?”

Do **not** use live `show` output to **discover** static edges on every cron tick — that belongs in the operational pass. Static rebuild is expensive and config-driven; operational refresh is lighter and runs against the known edge set.

### How they relate

- Static defines the **edge inventory** (`id`, `type`, `local`, `remote`, config `meta`).
- Operational attaches **runtime `state`** to those edge IDs (or marks config-only edges as `down` / `unknown` if live checks fail).
- **Issues** (unidirectional adjacency, config present but session down, out-of-sync device) come from comparing static intent to operational reality — computed each run, not stored in the static file.

```text
static edges (config)  +  operational state (live)  →  issues + per-layer summary
```

## State layout

```text
state/
  latest.json                  # service health snapshot baseline (existing)
  latest.meta.json             # run metadata (existing)
  topology.static.json         # persisted static topology (config-derived)
  topology.meta.json           # optional: rebuild reason, device fingerprints, last config check
  runs/<run-id>/
    snapshot.json              # full run: services, counts, topology (static + operational)
    delta.json                 # vs previous latest.json (existing)
    report.md                  # human report (existing)
```

Removed from v1: separate `runs/<run-id>/topology.json` — topology lives inside `snapshot.json`.

### `state/topology.static.json`

Config-derived graph only — no live up/down on edges (or omit `state` entirely):

```json
{
  "built_at": "2026-06-29T18:00:00Z",
  "build_reason": "config_change",
  "nodes": [
    {"id": "pe-den1", "site_id": null}
  ],
  "layers": {
    "physical": {
      "edges": [
        {
          "id": "if:pe-den1:Gi0/0/1",
          "type": "interface",
          "local": {"device": "pe-den1", "interface": "Gi0/0/1"},
          "remote": null,
          "meta": {"admin": "up"}
        }
      ],
      "summary": {"total": 1}
    },
    "underlay": {"edges": [...], "summary": {"total": 0}},
    "routing": {"edges": [...], "summary": {"total": 0}},
    "services": {"edges": [...], "summary": {"total": 0}}
  }
}
```

`build_reason`: `initial` | `config_change` | `force` | `device_added` | `device_removed` | `service_change`.

Static layer `summary` counts **configured** objects (`total`) plus **`by_device`** interface counts per node. Operational physical `summary.by_device` adds per-device up/down/degraded/unknown.

### When to rebuild static (write `state/topology.static.json`)

Rebuild **static topology from config** when **any** of:

1. **`state/topology.static.json` missing** (first run).
2. **Force flag** — CLI `--force-topology-update` and/or env `TOPOLOGY_FORCE_UPDATE=1`.
3. **Device config change** — for devices in the graph:
   - `compare_device_config` returns a non-empty diff, and/or
   - inventory/config implies interface/neighbor/peer change (new device, removed device, fewer interfaces, etc.).
4. **Device inventory change** — `list_devices` adds/removes a device compared to cached `nodes`.
5. **Service config change** — new/deleted service instance or `compare_service_config` indicates endpoint change.

If **none** of the above: **load** `state/topology.static.json`; skip static rediscovery.

**Operational refresh always runs** after static is loaded or rebuilt — independent of static rebuild triggers.

### Per-run flow

```text
load state/topology.static.json (or rebuild static from config if triggers fired)
    → operational refresh on static edge set (live status / adjacency / sync)
    → compute issues (static vs operational mismatch) + operational summary
    → snapshot["topology"] = { static, operational, static_source, static_built_at, operational_refreshed_at }
save_run_artifacts (optional topology copy under runs/<run-id>/)
```

- Static rebuild skipped → `static_source: "cache"`, copy `static.built_at` from file.
- Static rebuilt → `static_source: "fresh"`.

## Storage model

### What “canonical” means

The **canonical** copy of static topology is **`state/topology.static.json`** — the single authoritative file the collector uses between runs.

- On a normal run (no config change): **load static from this file**; do not rediscover from NSO.
- On config change or force update: **rebuild and overwrite this file**.
- Copies elsewhere (`runs/.../snapshot.json`, `latest.json`) are **historical records** of what was attached to a given run — useful for audit and delta — but the next run’s collector **starts from `topology.static.json`**, not from an old snapshot file.

Operational topology has **no canonical file**. It is collected fresh each run and only persists inside run snapshots and `latest.json` until the next run replaces them.

### What is stored where

| Location | Static | Operational | Role |
|----------|--------|-------------|------|
| **`state/topology.static.json`** | ✅ canonical | ❌ | Master config-derived graph; shared across runs |
| **`state/topology.meta.json`** | (metadata only) | ❌ | Optional: rebuild reason, fingerprints, last config check |
| **`state/runs/<run-id>/snapshot.json`** | ✅ embedded copy | ✅ this run | Full run record (services, counts, topology); self-contained |
| **`state/latest.json`** | ✅ from last run | ✅ from last run | Pointer for **next run’s delta** (same pattern as today’s service delta) |
| **`state/runs/<run-id>/delta.json`** | optional structural diff | ✅ status diff | Computed vs previous `latest.json` |

There is **no** `state/topology.operational.json`. Operational state is inherently per-run.

### `snapshot.json` contains both

Each `state/runs/<run-id>/snapshot.json` is the **full agent snapshot** for that run — not operational-only. Under `topology`:

```json
{
  "run_id": "2026-06-29T18:00:00Z",
  "services": { },
  "counts": { },
  "topology": {
    "static_source": "cache",
    "static_built_at": "2026-06-28T08:00:00Z",
    "operational_refreshed_at": "2026-06-29T18:00:00Z",
    "static": {
      "nodes": [ ],
      "layers": { }
    },
    "operational": {
      "layers": { },
      "issues": [ ],
      "summary": { }
    }
  }
}
```

How **static** gets into the snapshot:

- **`static_source: "fresh"`** — rebuilt this run; written to `topology.static.json`, then embedded in the snapshot.
- **`static_source: "cache"`** — loaded from `topology.static.json` and embedded anyway so the run file is **self-contained** (no need to open two files to inspect a past run).

**Operational** is always new for that run.

### Read/write flow across runs

```text
Run N
  load or rebuild → state/topology.static.json
  refresh operational (live)
  build snapshot["topology"] = { static, operational, … }
  write state/runs/<run-id>/snapshot.json
  write state/latest.json          ← full snapshot (existing publish behavior)

Run N+1
  previous ← state/latest.json
  load static from topology.static.json (or rebuild if triggered)
  refresh operational
  compute_delta(current, previous)
      operational: diff previous.topology.operational vs current.operational
      static:      diff previous.topology.static vs current.static (meaningful when static_source == "fresh")
  write new snapshot + latest.json
```

### Optional size optimization (later)

If static JSON grows large, runs with `static_source: "cache"` could store only `{ "static_built_at", "static_path" }` instead of inlining the full static object. **v1:** inline both static and operational in every snapshot for simplicity.

## Delta semantics

Two kinds of topology change; compare against **`previous` from `state/latest.json`** (same as service/count delta today).

| Delta kind | Question it answers | When it changes | Compare |
|------------|---------------------|-----------------|---------|
| **Operational** | What got worse or better since the last run? | Often — link/session flaps | `previous.topology.operational` vs `current.topology.operational` |
| **Static (structural)** | Did configured topology change in NSO? | Rare — only when static rebuilt | `previous.topology.static` vs `current.topology.static` |

**Operational delta** examples: ISIS adjacency was up → down; BGP session recovered; new issue “configured peer down”; device out of sync.

**Static delta** examples: device added/removed; interface or neighbor removed from config; new BGP peer in CDB; service endpoint changed.

Most scheduled runs have **operational delta only** (static unchanged, loaded from cache). That is expected.

Report output may include two optional blocks:

```text
Operational changes (since last run)
  - underlay: pe-den1 ↔ pe-chi1 down (was up)
  - routing: 1 recovery

Structural changes (static rebuilt this run)
  - +1 BGP peer pe-den1 → pe-nyc1
  - -1 interface pe-chi1 Gi0/0/3
```

Omit the structural block when `static_source == "cache"`.

## Design refinements

Agreed constraints for implementation:

1. **Static source of truth** — Prefer **NSO CDB / service models** for static edges, not live `show run` on the device. Keeps static stable when a box is out of sync with intent.
2. **Sync is operational** — `check_device_sync` / out-of-sync is an **operational** signal (“intent not fully applied”), not by itself a trigger to rebuild static. Rebuild static when **NSO intent** changes; report sync drift in `operational.issues`.
3. **Unexpected live objects** — If live checks find a neighbor/session **not** in static (manual box config, stale cache), emit a low-severity issue e.g. `code: "unexpected_live_object"` rather than silently ignoring.
4. **Optional max-age rebuild** — Not required for v1. Optionally rebuild static after N days even without detected config change, as a safety net against missed change detection.

## Force update

| Mechanism | Use |
|-----------|-----|
| `nso-summary-run --force-topology-update` | One-off full rebuild (debug, after manual NSO edits) |
| `TOPOLOGY_FORCE_UPDATE=1` in `.env` | Same, for a single scheduled run if needed |

Document in README when operators should force rebuild (e.g. after bulk device template push).

## Config-change detection (planned)

Lightweight check **each run** to decide if static rebuild is needed (operational refresh still runs regardless):

1. `list_devices` → compare to cached static `nodes`.
2. On inventory match: spot-check `compare_device_config` for changed devices (gate with fleet sync summary when possible).
3. Service layer: diff `get_services` instance IDs vs static service edges; use `compare_service_config` on additions/changes.

**Note:** device **out of sync** (`check_device_sync`) is an **operational** signal (config not fully applied on device), not necessarily a trigger to rebuild static — unless NSO CDB itself changed and static intent moved. Rebuild static when **intent/config in NSO** changes; report sync drift in operational `issues`.

Store optional **`topology.meta.json`** with per-device `last_static_check_at` and fingerprints.

**Open:** maximum age fallback (e.g. rebuild every 7 days even without detected change) — not required for v1; add if ops want a safety net.

## Non-goals (v1)

- Interactive map UI (Graphviz/HTML deferred until JSON is stable).
- Treating **site** (physical location/domain) as a required NSO object — MCP lists **devices**, not sites.
- Replacing existing **service health** in `snapshot["services"]` / `snapshot["counts"]`.

## Layer model

Bottom-up troubleshooting order:

| Layer key | Report title | Static (config) | Operational (live) |
|-----------|--------------|-----------------|---------------------|
| `physical` | Layer 0 — Physical | Configured interfaces | `get_interface_health`, oper/admin/errors |
| `underlay` | Layer 1 — Underlay IGP | Configured IS-IS interfaces/neighbors | `check_isis_adjacencies`, neighbor state |
| `routing` | Layer 2 — Routing (BGP) | Configured BGP peers | `verify_bgp_peer_reachability`, session state |
| `services` | Layer 3 — Services | Service endpoints from NSO | `check_service_sync`, existing `services` health |

Collect operational checks **bottom-up** when optimizing MCP calls: underlay failures may skip expensive BGP checks on unreachable devices.

## Snapshot shape (option B — layers)

Each run exposes `snapshot["topology"]` with **static** (from cache or fresh rebuild) and **operational** (always fresh this run):

```json
{
  "static_source": "cache",
  "static_built_at": "2026-06-28T08:00:00Z",
  "operational_refreshed_at": "2026-06-29T18:00:00Z",
  "coverage": {
    "devices_total": 12,
    "devices_queried": 10,
    "devices_failed": 2
  },
  "static": {
    "nodes": [{"id": "pe-den1", "site_id": null}],
    "layers": {
      "physical": {"edges": [], "summary": {"total": 0}},
      "underlay": {"edges": [], "summary": {"total": 0}},
      "routing": {"edges": [], "summary": {"total": 0}},
      "services": {"edges": [], "summary": {"total": 0}}
    }
  },
  "operational": {
    "layers": {
      "physical": {
        "edges": [],
        "summary": {"total": 0, "up": 0, "down": 0, "degraded": 0, "unknown": 0}
      },
      "underlay": {
        "edges": [],
        "summary": {"total": 0, "up": 0, "down": 0, "unidirectional": 0, "unknown": 0}
      },
      "routing": {
        "edges": [],
        "summary": {"total": 0, "up": 0, "down": 0, "unknown": 0}
      },
      "services": {
        "edges": [],
        "summary": {"total": 0, "up": 0, "down": 0, "degraded": 0, "unknown": 0}
      }
    },
    "issues": [],
    "summary": {
      "issues_total": 0,
      "issues_by_layer": {
        "physical": 0,
        "underlay": 0,
        "routing": 0,
        "services": 0
      }
    }
  }
}
```

Operational layer `edges` reference the same `id` as static edges and carry **`state`** (see below). If an operational edge has no matching static edge, flag as `code: "unexpected_live_object"` (optional v2).

### Design rules

- **`static.layers.*` always present** — configured edges; empty arrays and `{ "total": 0 }` when not implemented.
- **`operational.layers.*` always present** — runtime state per static edge id; up/down summaries here only.
- **`static.nodes`** — device inventory from config scope (`id` = device name). Optional `site_id` later.
- **`operational.issues`** — global list, sorted by severity; often static-vs-operational mismatch (configured peer down, unidirectional adjacency, device out of sync).
- **Do not duplicate** full service instance records under `static.layers.services`; link to `snapshot["services"]` via IDs where possible.

## Physical layer: nodes, `local`, and `remote`

**Devices are not physical edges.** They appear in top-level **`nodes`** (inventory for the whole topology). The **physical layer** lists **interfaces** — attachment points on each device.

| Concept | Where | Meaning |
|---------|--------|---------|
| **Device** | `nodes[].id` | NSO-managed box (e.g. `lbnl-data-sw`) |
| **Physical edge** | `layers.physical.edges[]` | One **configured interface** on one device |
| **`local`** | on each physical edge | `{ "device", "interface" }` — this port on this box |
| **`remote`** | on each physical edge | **`null` in v1** — no far-end device/interface on the link |

### Why `remote` is null at Layer 0

A physical edge answers: *“this interface exists in config on this device.”* It does **not** answer *“who is on the other end of the cable?”*

Links between devices are modeled in **higher layers**, where both ends are known from config:

| Layer | Edge type | `local` | `remote` |
|-------|-----------|---------|----------|
| **Physical** | `interface` | device + interface | **`null`** |
| **Underlay** | `isis_adjacency` | device + interface | neighbor device + interface |
| **Routing** | `bgp_session` | device + peer | peer device / address |
| **Services** | `l2vpn_endpoint`, … | service endpoint | far-end endpoint |

```text
Physical:   lbnl-data-sw  HundredGigE0/0/0/0.2402     remote = null   (port exists)

Underlay:   lbnl-data-sw  HundredGigE0/0/0/0.2402  ↔  renc-data-sw  HundredGigE0/0/0/0.2400
            (remote filled — live/config neighbor; names canonicalized — see below)
```

### Example: physical interface edge

Subinterfaces (e.g. `.2402`) use the same shape as physical interfaces:

```json
{
  "id": "if:lbnl-data-sw:HundredGigE0/0/0/0.2402",
  "type": "interface",
  "local": {
    "device": "lbnl-data-sw",
    "interface": "HundredGigE0/0/0/0.2402"
  },
  "remote": null
}
```

Operational state (admin/oper up/down, errors) is attached in **`operational.layers.physical.edges`** with the same `id`; it does not add a `remote` field.

### Per-device counts

Use **`layers.physical.summary.by_device`** (static: interface count; operational: up/down/degraded/unknown per device) instead of filtering the flat `edges` list by hand.

### Future (optional)

Point-to-point **links** could be modeled at physical layer with **`remote`** set (e.g. from LLDP, cable plan, or service config). v1 keeps physical edges as **single-ended interfaces** only.

## Underlay layer (IS-IS)

**Status:** **implemented** — `agent/topology/underlay.py`; static rebuild uses config + one live pairing pass; operational every run via `check_isis_adjacencies`.

Underlay edges are **relationships between two devices** — unlike physical interfaces, they have both **`local`** and **`remote`** (device + interface on each end).

### Static vs operational

| Version | Question | MCP tool(s) |
|---------|----------|-------------|
| **Static** | Which IS-IS adjacencies **should** exist (NSO intent)? | **`explore_nso_path`**, fallback **`get_device_config`** |
| **Operational** | Which adjacencies are **Up** right now? | **`check_isis_adjacencies`** per device (`show isis adjacency`) |

Same persistence rules as physical: static in `topology.static.json` (rebuild on config change); operational every run.

### What “IS-IS config path” means (NED vs live device)

This is **not** “run `show run` on the box.”

NSO stores each device’s **intended configuration** in CDB using a **NED** (Network Element Driver) — a YANG model for that platform (e.g. Cisco IOS-XR). The agent reads that via RESTCONF using paths like:

```text
tailf-ncs:devices/device=lbnl-data-sw/config/tailf-ned-cisco-ios-xr:router/isis
```

| Term | Meaning |
|------|---------|
| **Device config (in NSO)** | What NSO **should** deploy — CDB / `devices/device=…/config/…` — **source for static underlay** |
| **NED** | The YANG namespace in that path (`tailf-ned-cisco-ios-xr:…`, `tailf-ned-cisco-ios:…`, etc.) — determines **which subtree** holds IS-IS |
| **Live device** | What the router reports today — `check_isis_adjacencies`, `exec_show` — **source for operational underlay only** |

“The exact IS-IS config path” means: **which `…/config/<ned-module>:router/isis` (or equivalent) key exists for your platform** — we discover it once per fleet (like physical interface suffixes), not a CLI command you type on the router.

If the NED is IOS-XR, IS-IS usually lives under `router isis`; EOS/Junos use different YANG paths — the collector probes and caches the right suffix.

### Finding static IS-IS (config / intent)

Per device, same ladder as physical:

1. **`explore_nso_path`** depth 1 on `…/config` — find keys containing `isis` or `router`
2. **`explore_nso_path`** depth 2–3 on candidate paths, e.g.:
   - `tailf-ned-cisco-ios-xr:router/isis`
   - `tailf-ned-cisco-ios-xr:router/isis/interface`
3. Fallback: full **`explore_nso_path`** on `…/config` depth 3, then **`get_device_config`**

Extract from config:

- IS-IS enabled **interfaces** (interface name, level, circuit type in `meta`)
- Process / area / net (`meta`) where present

IOS-XR config often says **“IS-IS on this interface”** rather than naming the remote NSO device explicitly. Static edges may therefore be:

- **IS-IS-enabled interfaces** linked to physical `if:…` ids, and/or  
- **Explicit adjacency pairs** when config names a remote endpoint (NED-dependent)

### Finding operational IS-IS (live)

For each device in `nodes`:

```text
check_isis_adjacencies(device_name)
```

MCP runs `show isis adjacency` and parses rows:

```text
renc-data-sw   Hu0/0/0/0.2400   *PtoP*   Up   27   1d10h   ...
```

Fields used: **`system_id`** (neighbor id / hostname), **`interface`** (local if on this box), **`state`** (`Up`, `Init`, …).

This is **one direction only** — “on `lbnl-data-sw`, I see neighbor `renc-data-sw` on `Hu0/0/0/0.2402` as Up.”

### IOS-XR interface names: NSO config vs `show` output

**Expect different strings for the same interface.** This is normal IOS-XR behavior, not an NSO sync bug or MCP parsing error — but it **will break naive string compares** when correlating config to live state.

| Source | How the agent reads it | Example name |
|--------|------------------------|--------------|
| **NSO CDB / YANG config** | `explore_nso_path`, `get_device_config` — IOS-XR NED `interface-name` leaves | `HundredGigE0/0/0/0.2401`, `FortyGigE0/0/0/28.3981` |
| **Physical static layer** | Same config paths as above | `HundredGigE…` (long form) |
| **Live IS-IS** | `check_isis_adjacencies` → `show isis adjacency` — MCP parses CLI text **verbatim** | `Hu0/0/0/0.2401`, `Fo0/0/0/28.3981` |

**Why:**

1. **Config and YANG** use the **canonical modeled type** (`HundredGigE`, `FortyGigE`, `TenGigE`, `GigabitEthernet`, …). NSO stores what the NED models after sync/commit.
2. **Operational `show` commands** print **abbreviated display names** (`Hu`, `Fo`, `Te`, `Gi`, …) to fit CLI columns. `check_isis_adjacencies` does not expand them — it returns exactly what the router prints.

Common abbreviations (IOS-XR):

| Long (config / YANG) | Short (`show` output) |
|----------------------|------------------------|
| `HundredGigE` | `Hu` |
| `FortyGigE` | `Fo` |
| `TenGigE` | `Te` |
| `GigabitEthernet` | `Gi` |
| `FourHundredGigE` | `FH` |
| `Loopback` | `Lo` |

**Impact on topology collection:**

- Static underlay rebuild pairs live adjacencies against IS-IS **config** interface lists. Without normalization, every pair can fail validation (`live_adjacency_not_in_config`) and static `layers.underlay.edges` stays **empty** while operational shows edges — exactly the failure mode seen on the lab mesh.
- Edge IDs and stored endpoint names should use **one canonical form**. The agent resolves to the **physical-layer config name** when a match exists, otherwise expands abbreviated CLI names to long form (`agent/topology/interfaces.py`).

**Guidance for future layers / tools:**

- Any code that joins **NSO config paths** to **`exec_show` / `check_*` parsed output** must use `interfaces_match()` (or equivalent), not `==`.
- The same pattern may appear on other shows (`show interfaces summary`, BGP neighbor output, etc.) — probe before assuming one string format.
- Do **not** “fix” this in MCP by rewriting show output unless you also handle all platform variants; normalization at correlation time in the agent is safer.

### Correlating into bidirectional edges

```text
1. Collect operational rows from every device
2. Map system_id → NSO device name (hostname match; optional map table later)
3. Pair: A sees B on if-A  AND  B sees A on if-B  →  one underlay edge
4. Canonical id (endpoints ordered lexicographically; **long interface names** after normalization):
     isis:lbnl-data-sw:HundredGigE0/0/0/0.2402:renc-data-sw:HundredGigE0/0/0/0.2400
5. state.local / state.remote = per-direction oper state
```

**Issue codes (examples):**

| Code | Meaning |
|------|---------|
| `unidirectional_adjacency` | A sees B up; B does not see A (or down) |
| `live_adjacency_not_in_config` | Live pair rejected — often **abbreviated vs long interface name** before normalization; or IS-IS not configured on that interface |
| `configured_no_adjacency` | Static IS-IS on interface; no operational Up adjacency |
| `unexpected_live_object` | Live neighbor not present in static scope |
| `collection_error` | MCP / device unreachable for this device |

### MCP tools (underlay)

| Purpose | Tool |
|---------|------|
| Device inventory | `list_devices` |
| Static IS-IS intent | `explore_nso_path`, `get_device_config` |
| Operational adjacency | **`check_isis_adjacencies`** |
| Optional extra parse | `exec_show` (`isis adjacency` / `isis neighbors`) — only if needed |
| NED / platform hint | `get_device_platform`, `get_device_ned_ids` (probe which config suffix to use) |

Do **not** use LLM or live-only discovery for static rebuild.

### Implementation order

1. **`agent/topology/underlay.py`** — operational collector + cross-device pairing (fastest value on lab mesh)
2. Static IS-IS from `explore_nso_path` (config paths above)
3. Wire into `load_or_build_topology`; `summary.by_device` for underlay
4. **`--probe-underlay`** (mirror `--probe-physical`) for path debugging
5. Report / delta sections for underlay issues

### Underlay edge shape

```json
{
  "id": "isis:lbnl-data-sw:HundredGigE0/0/0/0.2402:renc-data-sw:HundredGigE0/0/0/0.2400",
  "type": "isis_adjacency",
  "local": {"device": "lbnl-data-sw", "interface": "HundredGigE0/0/0/0.2402"},
  "remote": {"device": "renc-data-sw", "interface": "HundredGigE0/0/0/0.2400"},
  "meta": {"level": "L2", "area": "49.0001"}
}
```

Operational state on the same `id` uses `state.local`, `state.remote`, and `state.status` (`up`, `down`, `unidirectional`, …).

## Routing layer (BGP)

**Status:** **implemented** — `agent/topology/routing.py`; static rebuild uses BGP neighbor config + one live pairing pass (`show bgp … summary` via `exec_show`); operational every run.

Routing edges are **BGP sessions between two devices** — both **`local`** and **`remote`** carry `{device, address}` (router-id / peering address).

### Static vs operational

| Version | Question | MCP tool(s) |
|---------|----------|-------------|
| **Static** | Which BGP peers **should** exist? | **`get_device_config`** (primary); fallback **`explore_nso_path`** |
| **Operational** | Which sessions are **Established** right now? | **`exec_show`** (`bgp ipv4 unicast summary`, fallback `bgp summary`) |

Optional reachability signal (not session state): **`verify_bgp_peer_reachability`** pings configured neighbors — useful for underlay issues but does not replace BGP FSM state.

### Finding static BGP (config / intent)

Per device, **primary source is full device config**:

```text
get_device_config(device_name)
```

The collector walks the returned YANG tree for `router/bgp` → `neighbor[]` (`id`, `remote-as`) and `router-id`. One MCP call per device.

**Fallback** (only if `get_device_config` fails or returns no BGP): targeted `explore_nso_path` on `…/router/bgp` (IOS-XR: `tailf-ned-cisco-ios-xr:router/bgp`).

#### Why `get_device_config` instead of `explore_nso_path` (BGP)

We **prefer `get_device_config()`** for BGP peering intent, not a multi-step `explore_nso_path` ladder.

| Approach | Problem |
|----------|---------|
| **`explore_nso_path` with low depth** | BGP neighbors live deep under `…/config/…/router/bgp/bgp-no-instance/neighbor`. Shallow probes (depth 1–2) often return **empty lists** (`"neighbor": []`) or omit nested keys — looks like “no BGP” when peers exist. |
| **`explore_nso_path` with higher depth** | Works only if you guess the right path **and** depth for each NED. IOS-XR may need depth 3+; wrong depth still truncates. Multiple probes per device (discover suffix → try depth 2 → try depth 3) are slow and brittle. |
| **`get_device_config`** | Returns the **full device config tree** in one call. The collector walks it for any `router/bgp` subtree and all `neighbor` entries — no depth tuning, no false-empty neighbor lists from truncated exploration. |

**Operational note:** `explore_nso_path` responses can also mark large subtrees as **`truncated`**, which the BGP parser treats as “no neighbors” — another source of incomplete peering data.

**Contrast with underlay (IS-IS):** IS-IS interface lists are smaller and closer to the surface, so `explore_nso_path` + depth probing is acceptable there. BGP neighbor tables are deeper and denser; full config is the reliable source.

`explore_nso_path` remains a **fallback** when `get_device_config` is unavailable or errors, and for `--probe-routing` debugging (see `attempts[]` in probe output).

IOS-XR config lists **neighbor addresses**, not remote hostnames. Pairing uses **router-id** (config or live summary line `BGP router identifier …`) to resolve which device owns a neighbor IP.

On static rebuild, one live **`exec_show`** pass pairs sessions (same pattern as underlay `seed_from_live`).

### Finding operational BGP (live)

For each device:

```text
exec_show(device_name, input_command="bgp ipv4 unicast summary")
```

Parse neighbor rows for **Established** / **Idle** / **Active** / … — same bidirectional pairing as underlay:

```text
1. Collect summary rows from every device
2. Map neighbor IP → NSO device via router-id table
3. Pair: A sees B at IP-X Established AND B sees A at IP-Y Established → one routing edge
4. Canonical id (addresses then devices, lexicographically ordered):
     bgp:10.0.0.1:10.0.0.2:lbnl-data-sw:renc-data-sw
5. state.local / state.remote = per-direction BGP FSM state
```

**Issue codes (examples):**

| Code | Meaning |
|------|---------|
| `one_sided_session` | A Established; B not Established (or vice versa) |
| `live_session_not_in_config` | Live pair not reflected in BGP neighbor config |
| `configured_no_session` | Static session; no operational Established state |
| `unknown_neighbor_address` | Neighbor IP could not be mapped to an NSO device |
| `unexpected_live_object` | Live session not in static topology |
| `no_configured_bgp` | Device has no BGP neighbors in NSO config |

### Routing edge shape

```json
{
  "id": "bgp:10.0.0.1:10.0.0.2:lbnl-data-sw:renc-data-sw",
  "type": "bgp_session",
  "local": {"device": "lbnl-data-sw", "address": "10.0.0.1"},
  "remote": {"device": "renc-data-sw", "address": "10.0.0.2"},
  "meta": {"local_as": 398900, "remote_as": 398900}
}
```

Operational `state.status`: `up` (both Established), `down` (neither), `degraded` (one-sided).

Probe: `nso-summary-run --probe-routing [DEVICE]`.

## Edge object

### Static edge (config)

Stored in `static.layers.*.edges`:

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Stable across runs (see ID convention) |
| `type` | yes | e.g. `interface`, `isis_adjacency`, `bgp_session`, `l2vpn_endpoint` |
| `local` | yes | Near-end from config (device + layer-specific fields, e.g. interface name) |
| `remote` | yes or null | Far-end from config when the relationship is **between two endpoints**; **`null` for physical `interface` edges** (see [Physical layer: local and remote](DESIGN.md#physical-layer-nodes-local-and-remote)) |
| `meta` | no | Config-only (admin state, IS-IS level, ASN, service type, …) |

No operational `state` on static edges.

### Operational edge (runtime)

Stored in `operational.layers.*.edges` — same `id` as static:

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Matches static edge |
| `state` | yes | Live status (see below) |
| `meta` | no | Runtime extras (last flap, error text, sync detail) |

### `state.status` values

| Value | Meaning |
|-------|---------|
| `up` | Healthy / established / both sides agree |
| `down` | Known failed |
| `degraded` | Partial (e.g. one direction up, policy issue) |
| `unidirectional` | A sees B but not vice versa (or mismatch) |
| `unknown` | Could not determine (MCP error, timeout, parse failure) |

### Edge ID convention

Canonical string for delta and `issues[].edge_id`:

```text
{type-prefix}:{endpoint-a}:{endpoint-b}
```

Endpoints ordered lexicographically where symmetric (e.g. device names) so A↔B and B↔A share one id.

Examples:

```text
isis:pe-chi1:Gi0/0/1:pe-den1:Gi0/0/1
bgp:10.0.0.1:10.0.0.2:pe-den1:pe-chi1
if:pe-den1:Gi0/0/1
svc:l2ptp:customer-a:endpoint:sw1:Eth1
```

## Issue object

```json
{
  "severity": "high",
  "layer": "underlay",
  "code": "unidirectional_adjacency",
  "edge_id": "isis:pe-den1:Gi0/0/1:pe-chi1:Gi0/0/1",
  "message": "pe-den1 Gi0/0/1 sees up; pe-chi1 Gi0/0/1 sees down",
  "root_cause_layer": "underlay"
}
```

`severity`: `high` | `medium` | `low` (exact policy TBD).

Cross-layer issues (e.g. service down because IS-IS failed) may set `root_cause_layer` below the failing service.

## Sites (optional, later)

A **site** is a physical location/domain with multiple devices. NSO MCP provides **`list_devices`** and **`get_device_groups`**, not a first-class site list.

- v1: omit `site_id` or leave `null` on nodes.
- Later: optional `sites.yaml` or hostname rules; set `nodes[].site_id` for inter-site rollups in reports/metrics.

Inter-site reporting is derived from node labels, not a separate site catalog in the snapshot.

## MCP tool mapping

| Layer | Static (config) | Operational (live) | Static rebuild trigger |
|-------|-----------------|--------------------|-------------------------|
| Inventory | `list_devices` | `get_fleet_sync_summary`, `check_device_sync` | device add/remove |
| Config change | `explore_nso_path`, device/service config | — | `compare_device_config`, `compare_service_config` |
| Physical | interface config via YANG/`exec_show` | `get_interface_health` | interface add/remove in config |
| Underlay | IS-IS config paths | `check_isis_adjacencies`, neighbor shows | neighbor/interface change in config |
| Routing | BGP neighbor config | `verify_bgp_peer_reachability`, BGP shows | peer change in config |
| Services | `get_services`, service YANG | `check_service_sync`, `agent/health.py` | service create/delete/endpoint change |

Generic exploration: `explore_nso_path` for YANG paths not wrapped by dedicated tools.

Transport: **stdio MCP** via `agent/mcp_client.py` (`StdioTransport` → `cisco-nso-mcp-server` subprocess).

## Package layout (planned)

```text
agent/topology/
  README.md
  docs/
    README.md
    DESIGN.md
  __init__.py          # load_or_build_topology(...) entry
  collect.py           # static rebuild vs cache; operational refresh
  persist.py           # read/write state/topology.static.json, change detection
  graph.py             # edge ids, summaries, shared types
  static/              # optional: per-layer static collectors
  operational/         # optional: per-layer live status collectors
  physical.py
  underlay.py          # IS-IS
  routing.py           # BGP
  services.py
```

`agent/collect.py` calls `topology.load_or_build(...)` and assigns `{ static, operational, … }` to `snapshot["topology"]`.

## Integration with summary agent

```text
collect_snapshot()
  ├── existing: services, counts, fleet_sync
  └── topology.load_or_build(client, settings, state_dir)
          ├── if static rebuild needed → collect static from config → write state/topology.static.json
          └── else → load state/topology.static.json
          → always: operational refresh on static edge ids
          → compute issues (config vs reality)
          → snapshot["topology"]

compute_delta()
  ├── service/count delta vs previous snapshot (existing)
  └── operational delta vs previous snapshot["topology"]["operational"]
      static delta only when static was rebuilt (compare edge sets / build_reason)

report_format / summarize
  └── deterministic layer sections + optional LLM on issues narrative

publish / metrics (future)
  └── nso_topology_{layer}_edges_down, issues_total, topology_static_stale, …
```

Replace the current placeholder:

```python
"topology": {"isis_failed": None, "bgp_down": None}
```

## Report format (deterministic)

```text
Topology summary
----------------
Issues: 2 (underlay: 1, routing: 1)

Layer 3 — Services
------------------
...

Layer 2 — Routing (BGP)
-----------------------
...

Layer 1 — Underlay (IS-IS)
--------------------------
...

Layer 0 — Physical
------------------
...
```

Slack/email use **plain text** tables; terminal/`report.md` may use markdown (same structure as today’s service counts).

## Viewing topology

Operators view topology via **reports**, **JSON** under `STATE_DIR` (default `./state`), or a **static Graphviz** image from `scripts/export_topology_dot.py`. There is **no interactive map UI** in v1.

### Where data lives

| Goal | File | Notes |
|------|------|--------|
| Human-readable summary | `state/runs/<run-id>/report.md` | Path via `state/latest.meta.json` |
| Latest static + operational | `state/latest.json` | Key `.topology` — used for next-run delta |
| Config-only (canonical static) | `state/topology.static.json` | What NSO config says should exist |
| Specific past run | `state/runs/<run-id>/snapshot.json` | Self-contained: `.topology.static` + `.topology.operational` |
| Changes vs previous run | `state/runs/<run-id>/delta.json` | Includes topology deltas when static/operational change |
| Graphviz DOT / image | `scripts/export_topology_dot.py` | See below |

### Commands

From the project root (adjust if `STATE_DIR` in `.env` points elsewhere):

```bash
# Latest report location
cat state/latest.meta.json

# Topology summary from last successful run
python3 -c "
import json
t = json.load(open('state/latest.json'))['topology']
print('static_source:', t.get('static_source'))
print('static_built_at:', t.get('static_built_at'))
print('operational_refreshed_at:', t.get('operational_refreshed_at'))
issues = t.get('operational', {}).get('issues', [])
print('issues:', len(issues))
for i in issues[:10]:
    print(' -', i.get('severity'), i.get('layer'), i.get('message'))
for layer in ('physical', 'underlay', 'routing', 'services'):
    s = t.get('operational', {}).get('layers', {}).get(layer, {}).get('summary', {})
    if s:
        print(layer, s)
"

# Canonical configured graph (static only)
python3 -m json.tool state/topology.static.json | less
```

### Graphviz export

`scripts/export_topology_dot.py` reads `state/topology.static.json` (or `-i state/latest.json`) and writes Graphviz DOT for device↔device edges. Default layers: **underlay**, **routing**.

Prerequisite for images: install [Graphviz](https://graphviz.org/) so the `dot` binary is on `PATH` (`brew install graphviz` on macOS).

**Script renders for you** (`--render` invokes `dot`):

```bash
python3 scripts/export_topology_dot.py -o topo.dot --render png
python3 scripts/export_topology_dot.py -o topo.dot --render svg
```

**Equivalent: script writes DOT, you call `dot`:**

```bash
python3 scripts/export_topology_dot.py -o topo.dot
dot -Tpng topo.dot -o topo.png
dot -Tsvg topo.dot -o topo.svg
```

`--render png` is the same as `dot -Tpng topo.dot -o topo.png` after the `.dot` file exists.

Useful flags: `--layers underlay,routing,physical`, `--include-unlinked` (physical/inventory endpoints with no remote). Full CLI help: `python3 scripts/export_topology_dot.py -h`.

### Debug collection (stdout)

```bash
# One run without updating latest.json
nso-summary-run --skip-llm --dry-run 2>/dev/null | python3 -c "
import json, sys
t = json.load(sys.stdin)['snapshot']['topology']
print(json.dumps(t, indent=2))
"
```

### List underlay / BGP edges (example)

After implementation, filter operational edges by layer:

```bash
python3 -c "
import json
topo = json.load(open('state/latest.json'))['topology']
for e in topo.get('operational', {}).get('layers', {}).get('underlay', {}).get('edges', []):
    st = e.get('state', {})
    print(st.get('status', '?'), e.get('id'))
"
```

### Slack / email

Scheduled runs include topology sections in the same delivery as service health — plain text in Slack, plain + HTML in email, markdown in saved `report.md`.

### Later options (not v1)

| Option | Status |
|--------|--------|
| Grafana panels (`nso_topology_*` metrics) | Phase 5 — stack in `deploy/monitoring/` |
| Graph export (Graphviz, HTML) | Phase 5 |
| Interactive map UI | Out of scope for v1 |
| Cursor + NSO MCP | Available now for ad-hoc device/service queries |

## Phasing

| Phase | Deliverable | Status |
|-------|-------------|--------|
| **1a** | Static/operational split, `topology.static.json`, **physical** layer | Done |
| **1b** | **Underlay** (IS-IS) — [design](DESIGN.md#underlay-layer-is-is) | Done |
| **2** | **routing** (BGP) | **Done** — `agent/topology/routing.py` |
| **3** | **Services** layer; sync as operational issues | **Done** — `agent/topology/services.py` |
| **4** | Delta (operational run-over-run), Grafana metrics | Planned |
| **5** | Graph export, optional visualization | Planned |

## Partial failure

- MCP errors on one device must not abort the whole snapshot.
- Record `coverage.devices_failed` and mark affected edges `unknown`.
- Include device/query errors in `issues` with `code: "collection_error"` when appropriate.

## Open decisions

- Exact BGP/IS-IS **live CLI** variants per NED (IOS-XR vs EOS vs Junos) — normalize in layer modules; **static** paths discovered via `explore_nso_path` (see [underlay](DESIGN.md#underlay-layer-is-is)).
- Whether `static.layers.services.edges` represent **service instances** vs **endpoint pairs** — **decided: endpoint pairs** (see `docs/superpowers/specs/2026-07-28-services-topology-layer-design.md`).
- Severity rules and alert thresholds for Slack vs Grafana.
- Whether `compare_device_config` on every device each run is too heavy — may gate on fleet sync summary first.

## Example: static underlay edge

```json
{
  "id": "isis:pe-den1:Gi0/0/1:pe-chi1:Gi0/0/1",
  "type": "isis_adjacency",
  "local": {"device": "pe-den1", "interface": "Gi0/0/1"},
  "remote": {"device": "pe-chi1", "interface": "Gi0/0/1"},
  "meta": {"level": "L2", "area": "49.0001"}
}
```

## Example: operational state for same edge

```json
{
  "id": "isis:pe-den1:Gi0/0/1:pe-chi1:Gi0/0/1",
  "state": {
    "local": "up",
    "remote": "down",
    "status": "unidirectional"
  }
}
```

## Example: issue

```json
{
  "severity": "high",
  "layer": "underlay",
  "code": "unidirectional_adjacency",
  "edge_id": "isis:pe-den1:Gi0/0/1:pe-chi1:Gi0/0/1",
  "message": "Configured adjacency: pe-den1 Gi0/0/1 ↔ pe-chi1 Gi0/0/1; pe-den1 sees up, pe-chi1 sees down"
}
```
