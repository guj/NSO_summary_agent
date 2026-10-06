# Topology facts

Deterministic collection of the layered network model: **physical** (interfaces),
**underlay** (IS-IS) and **routing** (BGP). Each layer has a static view from NSO
configuration and an operational view from live device state.

Design and rationale: [docs/DESIGN.md](docs/DESIGN.md). Before joining configuration to
live `show` output, read
[IOS-XR interface names](docs/DESIGN.md#ios-xr-interface-names-nso-config-vs-show-output).
