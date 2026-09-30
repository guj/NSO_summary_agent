# Topology design docs

Documentation for the NSO summary agent **topology** feature — layered graph collection via the Cisco NSO MCP server.

| Document | Contents |
|----------|----------|
| [DESIGN.md](DESIGN.md) | … **[routing / BGP](DESIGN.md#routing-layer-bgp)**, [underlay / IS-IS](DESIGN.md#underlay-layer-is-is), … |

## Key design choices

**Two topology versions:**

- **Static** — config intent; canonical file **`state/topology.static.json`**; rebuilt on config change or force update.
- **Operational** — live status every run; **no** separate canonical file; stored in each **`snapshot.json`** and **`latest.json`**.

**Each `state/runs/<run-id>/snapshot.json`** embeds **both** static and operational under `topology`, plus services/counts as today.

**Delta:** operational diff run-over-run via `latest.json`; static diff only when static was rebuilt this run.

**Underlay:** IS-IS static + operational — [design](DESIGN.md#underlay-layer-is-is). **IOS-XR config vs `show` interface names** — [must-read](DESIGN.md#ios-xr-interface-names-nso-config-vs-show-output). Probe: `nso-summary-run --probe-underlay [DEVICE]`.

**Routing:** BGP static + operational — [design](DESIGN.md#routing-layer-bgp). Static BGP intent uses **`get_device_config`** (not `explore_nso_path`) — [why](DESIGN.md#why-get_device_config-instead-of-explore_nso_path-bgp). Probe: `nso-summary-run --probe-routing [DEVICE]`.

**Physical layer:** devices are **`nodes`**; each **`layers.physical` edge** is one interface (`local`); **`remote` is null** — see [Physical layer: nodes, local, and remote](DESIGN.md#physical-layer-nodes-local-and-remote).

**Viewing:** [DESIGN.md — Viewing topology](DESIGN.md#viewing-topology) — `report.md`, JSON, and Graphviz via `scripts/export_topology_dot.py` (or plain `dot` on the generated `.dot` file).

## Status

**Layer 0 (physical)**, **Layer 1 (underlay / IS-IS)**, and **Layer 2 (routing / BGP)** implemented. **Services** layer is next.

## Related

- [WORK_PLAN.md](../../../WORK_PLAN.md) — original roadmap (IS-IS/BGP bidirectional checks, Grafana trends)
- [agent/collect.py](../../collect.py) — orchestrates MCP collection today
- [agent/mcp_client.py](../../mcp_client.py) — stdio MCP client
