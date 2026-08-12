# Topology collection

Layered topology: **static** (config, canonical in `state/topology.static.json`) + **operational** (live, per run).

Layer 0 edges are **interfaces** on a device (`local`); **`remote` is null** — links to neighbors appear in underlay/routing. See [docs/DESIGN.md — Physical layer](docs/DESIGN.md#physical-layer-nodes-local-and-remote). Per-run snapshots: [storage & viewing](docs/DESIGN.md#storage-model).

**Design documentation:** [docs/README.md](docs/README.md)

Implementation: **physical** + **underlay (IS-IS)** + **routing (BGP)**. See [docs/DESIGN.md](docs/DESIGN.md). When joining config to live `show` output, read [IOS-XR interface names](docs/DESIGN.md#ios-xr-interface-names-nso-config-vs-show-output).
