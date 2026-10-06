# Topology facts layer design

How `nso_facts/topology/` models the network: which relationships it discovers from NSO
configuration, how it checks them against live device state, and why the collection works
the way it does. Collection is deterministic (Python and MCP); the LLM does not invent
adjacencies or peerings.

## Static vs operational

Two versions of the same layered model (`physical`, `underlay`, `routing`, `services`):

| Version | Meaning | Source | When updated |
|---------|---------|--------|--------------|
| **Static** | What **config says** should exist — devices, interfaces, IS-IS adjacencies and BGP peers as **configured** in NSO CDB / device intent | Config-oriented MCP (`explore_nso_path`, `get_device_config`) | Each run, from configuration |
| **Operational** | What is **true right now** — admin/protocol state, established adjacencies, BGP sessions | Live MCP (`check_isis_adjacencies`, `exec_show`) | Each run, from live state |

**Static** answers: “What is supposed to be connected?”  
**Operational** answers: “Is it up, synced, and working as configured?”

Static edges come from configuration, not from live `show` output. The one exception is pairing: configuration often names a neighbor address or an IS-IS interface without saying which device is on the other end, so a single live pass is used to pair the two ends (see the underlay and routing sections).

### How they relate

- Static defines the **edge inventory** (`id`, `type`, `local`, `remote`, config `meta`).
- Operational attaches **runtime `state`** to those edge IDs (or marks config-only edges as `down` / `unknown` if live checks fail).
- **Issues** (unidirectional adjacency, config present but session down, out-of-sync device) come from comparing static intent to operational reality — computed each run.

```text
static edges (config)  +  operational state (live)  →  issues + per-layer summary
```

## Design refinements

Constraints the implementation follows:

1. **Static source of truth** — Prefer **NSO CDB / service models** for static edges, not live `show run` on the device. Keeps static stable when a box is out of sync with intent.
2. **Sync is operational** — out-of-sync is an **operational** signal (“intent not fully applied”). It does not change the static edge set.
3. **Unexpected live objects** — If live checks find a neighbor/session **not** in static (manual box config), emit a low-severity issue e.g. `code: "unexpected_live_object"` rather than silently ignoring.

## Layer model

Bottom-up troubleshooting order:

| Layer key | Layer | Static (config) | Operational (live) |
|-----------|-------|-----------------|---------------------|
| `physical` | 0 — Physical | Configured interfaces | `exec_show` (`interfaces brief`): admin and line-protocol state |
| `underlay` | 1 — Underlay IGP | Configured IS-IS interfaces/neighbors | `check_isis_adjacencies`: neighbor state |
| `routing` | 2 — Routing (BGP) | Configured BGP peers | `exec_show` (`bgp ipv4 unicast summary`): session state |
| `services` | 3 — Services | Service endpoint pairs | Left empty by the diagnostic runner; service evidence lives in the case and is drawn in the report's service topology |

## Topology structure

Each run builds one structure with a **static** half (configuration) and an **operational** half (live state). The diagnostic runner assembles it from the run's evidence in `diagnostic_mas/device_health.py`:

```json
{
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
    "issues": []
  }
}
```

Operational layer `edges` reference the same `id` as static edges and carry **`state`** (see below). An operational edge with no matching static edge is flagged with `code: "unexpected_live_object"`.

### Design rules

- **`static.layers.*` always present** — configured edges; empty arrays when a layer has no data.
- **`operational.layers.*` always present** — runtime state per static edge id; up/down summaries here only.
- **`static.nodes`** — device inventory (`id` = device name).
- **`operational.issues`** — one list for all layers; often a static-vs-operational mismatch (configured peer down, unidirectional adjacency).

## Physical layer: nodes, `local`, and `remote`

**Devices are not physical edges.** They appear in top-level **`nodes`** (inventory for the whole topology). The **physical layer** lists **interfaces** — attachment points on each device.

| Concept | Where | Meaning |
|---------|--------|---------|
| **Device** | `nodes[].id` | NSO-managed box (e.g. `lbnl-data-sw`) |
| **Physical edge** | `layers.physical.edges[]` | One **configured interface** on one device |
| **`local`** | on each physical edge | `{ "device", "interface" }` — this port on this box |
| **`remote`** | on each physical edge | **always `null`** — no far-end device/interface on the link |

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

## Underlay layer (IS-IS)

Implemented in `nso_facts/topology/underlay.py`. Static edges come from configuration plus one live pairing pass; operational state comes from `check_isis_adjacencies`.

Underlay edges are **relationships between two devices** — unlike physical interfaces, they have both **`local`** and **`remote`** (device + interface on each end).

### Static vs operational

| Version | Question | MCP tool(s) |
|---------|----------|-------------|
| **Static** | Which IS-IS adjacencies **should** exist (NSO intent)? | **`explore_nso_path`**, fallback **`get_device_config`** |
| **Operational** | Which adjacencies are **Up** right now? | **`check_isis_adjacencies`** per device (`show isis adjacency`) |

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
| `TwentyFiveGigE` | `TF` |
| `Loopback` | `Lo` |
| `BVI` | `BV` |

**Impact on topology collection:**

- Building static underlay edges pairs live adjacencies against IS-IS **config** interface lists. Without normalization, every pair can fail validation (`live_adjacency_not_in_config`) and static `layers.underlay.edges` stays **empty** while operational shows edges.
- Edge IDs and stored endpoint names should use **one canonical form**. The agent resolves to the **physical-layer config name** when a match exists, otherwise expands abbreviated CLI names to long form (`nso_facts/topology/interfaces.py`).

**Guidance for future layers / tools:**

- Any code that joins **NSO config paths** to **`exec_show` / `check_*` parsed output** must use `interfaces_match()` (or equivalent), not `==`.
- The same pattern may appear on other shows (`show interfaces summary`, BGP neighbor output, etc.) — probe before assuming one string format.
- Do **not** “fix” this in MCP by rewriting show output unless you also handle all platform variants; normalization at correlation time in the agent is safer.

### Correlating into bidirectional edges

```text
1. Collect operational rows from every device
2. Map system_id → NSO device name (hostname match)
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

Static edges are never taken from the LLM or from live-only discovery.

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

Implemented in `nso_facts/topology/routing.py`. Static edges come from BGP neighbor configuration plus one live pairing pass (`show bgp … summary` via `exec_show`); operational state comes from the same command.

Routing edges are **BGP sessions between two devices** — both **`local`** and **`remote`** carry `{device, address}` (router-id / peering address).

### Static vs operational

| Version | Question | MCP tool(s) |
|---------|----------|-------------|
| **Static** | Which BGP peers **should** exist? | **`get_device_config`** (primary); fallback **`explore_nso_path`** |
| **Operational** | Which sessions are **Established** right now? | **`exec_show`** (`bgp ipv4 unicast summary`, fallback `bgp summary`) |

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

`explore_nso_path` remains a **fallback** when `get_device_config` is unavailable or errors.

IOS-XR config lists **neighbor addresses**, not remote hostnames. Pairing uses **router-id** (config or live summary line `BGP router identifier …`) to resolve which device owns a neighbor IP.

When static edges are built, one live **`exec_show`** pass pairs sessions (same pattern as underlay `seed_from_live`).

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
| `admin-down` | Administratively shut (physical layer) |

### Edge ID convention

Canonical string, also used in `issues[].edge_id`:

```text
{type-prefix}:{endpoint-a}:{endpoint-b}
```

Endpoints ordered lexicographically where symmetric (e.g. device names) so A↔B and B↔A share one id.

Examples:

```text
isis:pe-chi1:Gi0/0/1:pe-den1:Gi0/0/1
bgp:10.0.0.1:10.0.0.2:pe-den1:pe-chi1
if:pe-den1:Gi0/0/1
svc:l2ptp:customer-a:pe-chi1:pe-den1
```

## Issue object

```json
{
  "severity": "high",
  "layer": "underlay",
  "code": "unidirectional_adjacency",
  "edge_id": "isis:pe-den1:Gi0/0/1:pe-chi1:Gi0/0/1",
  "message": "pe-den1 Gi0/0/1 sees up; pe-chi1 Gi0/0/1 sees down"
}
```

`severity`: `high` | `medium` | `low`.

## MCP tool mapping

| Layer | Static (config) | Operational (live) |
|-------|-----------------|--------------------|
| Inventory | `list_devices` | — |
| Physical | `explore_nso_path`, fallback `get_device_config` | `exec_show` (`interfaces brief`) |
| Underlay | `explore_nso_path`, fallback `get_device_config` | `check_isis_adjacencies` |
| Routing | `get_device_config`, fallback `explore_nso_path` | `exec_show` (`bgp ipv4 unicast summary`, fallback `bgp summary`) |

Transport: stdio MCP via `nso_facts/mcp_client.py` (`StdioTransport` → `cisco-nso-mcp-server` subprocess).

## Modules

| Module | Role |
|--------|------|
| `devices.py` | Device names from `list_devices` responses |
| `interfaces.py` | Matching long (config) and short (`show`) interface names |
| `iface_equiv.py` | Admin-confirmed NSO-to-device interface name mappings |
| `graph.py` | Edge IDs and per-layer summaries |
| `physical.py`, `underlay.py`, `routing.py` | Static and operational collectors for each layer |
| `collect.py`, `persist.py`, `services.py`, `route_summary.py` | Whole-topology build with an on-disk static cache, from the earlier summary runner; not called by `nso-diagnostic-run` |

## Partial failure

- MCP errors on one device must not abort the whole run.
- Affected edges are marked `unknown`.
- Device and query errors are added to `issues` with `code: "collection_error"`.

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
