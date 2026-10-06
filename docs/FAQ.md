# FAQ — NSO diagnostic report

Common questions about reading the report from `nso-diagnostic-run`. To install and run the
agent, see [DIAGNOSTIC_RUNNER.md](DIAGNOSTIC_RUNNER.md).

## The report

### What sections does the report have?

In order: **Summary**, **Changes since previous run**, **Devices**, **Services**,
**Recommended follow-up**, **Run configuration** and **Run details**. The Summary is written by
the LLM and is omitted with `--skip-llm`. Findings from LLM investigations appear under the
service they concern; the rest of the report is deterministic.

The HTML report adds **Service topology** and **Routing topology**. Only Services and Recommended
follow-up start expanded; open any other section by clicking its heading or its link in the
navigation bar. The search box filters by service ID, device, type or evidence text.

### How do I see the topology?

Open the HTML report and expand **Routing topology** (IS-IS and BGP relationships between devices)
or **Service topology** (which devices each service attaches to, coloured by final status). The
legend and limits are described under
[Reports and topology](DIAGNOSTIC_RUNNER.md#reports-and-topology). There is no separate graph export.

---

## Devices

### What does the Devices section show?

One block per device:

```text
### site-a-data-sw

**NSO sync:** In sync
**Health:** Interface and hardware checks reported healthy
**Routing:** BGP 2/2 up · IS-IS 2/2 up
```

An **Attention** line or list follows when something about that device needs a look. For the
detailed per-device analysis, run with `--full`; it adds **Appendix: Detailed Device Analysis**.

### What do the NSO sync values mean?

| Value | Meaning |
|-------|---------|
| `In sync` | NSO reports the device configuration in sync |
| `Unknown — sync verification failed (…)` | The sync check errored or gave no answer. This is not evidence of drift or of an outage |
| `Not collected` | No sync result was gathered for this device in this run |
| Anything else, such as `locked` or `out-of-sync` | NSO's own result, shown as returned |

### How do I read the Health line?

`Interface and hardware checks reported healthy` means nothing was flagged. Otherwise the line lists
what was observed, then two labels:

```text
**Health:** NSO↔device interface mapping unconfirmed; control-plane drop counter=1,234 recorded. Labels: interfaces=Inventory Review; hardware=Review. Review is a triage flag from this run's checks — do not treat it as a confirmed hardware fault (use --full).
```

| Observation | Meaning |
|-------------|---------|
| `NSO↔device interface mapping unconfirmed` | At least one interface in NSO configuration could not be matched to a live interface on the device (label `interfaces=Inventory Review`) |
| `interface admin/oper up/down observed` | At least one configured interface is administratively up with its line protocol down |
| `control-plane drop counter=N recorded` | The device's control-plane drop counters add up to N (label `hardware=Review`) |
| `temperature sensor not ok`, `fan status not ok`, `power supply status not ok` | The hardware-health query returned a non-normal reading |

`hardware=Unavailable` means no usable hardware-health data was returned for the device.
`Interface and hardware status not collected for this run` means neither was gathered.

### Why do so many devices show `hardware=Review`?

Any non-zero control-plane drop counter sets it, and those counters are cumulative: one old burst
keeps the flag until the counters are cleared on the device. Treat it as a prompt to look, not as a
fault. Historical drop counters alone are not urgent.

### What does "interface mapping unconfirmed" mean, and how do I clear it?

An interface exists in NSO configuration but was not found among the device's live interfaces.
Common causes:

1. **Speed or breakout drift.** NSO still has `FourHundredGigE…/32` while the device shows `Hu…/32`
   or breakout members such as `Te…/36/0–3`.
2. **Stale configuration.** NSO lists a port the device does not have.
3. **Collection error.** The live interface query failed for that device.

For case 1, an administrator can record the confirmed NSO-to-device name mapping in a JSON file
and point `INTERFACE_EQUIVALENCES_FILE` at it. The schema is shown in
`config/interface-equivalences.example.json`. For case 2, correct the NSO configuration instead.

### Where do names like `HundredGigE0/0/0/9` and `Hu0/0/0/9` come from?

The long form comes from NSO device configuration. The short form is what the device prints in live
`show` output. The agent matches the two with fixed prefix maps (`HundredGigE`↔`Hu`,
`TwentyFiveGigE`↔`TF`, `BVI`↔`BV`, and so on), so both forms can appear in evidence.

### How are the BGP and IS-IS counts calculated?

`BGP 2/2 up` means two sessions between this device and other devices in the NSO inventory, both
up. Neighbors outside the NSO inventory are not counted, so the number can be lower than what
`show bgp summary` lists on the device. Those neighbors appear under Attention instead:

```text
**Attention:** BGP neighbor could not be mapped to NSO inventory. site-a-data-sw 192.0.2.1: could not map neighbor address to NSO device
```

`N/A` means no sessions to inventory devices were found for that protocol.

### What does "(initial; peer live checks unavailable)" mean?

```text
**Routing:** BGP 2/2 up (initial; peer live checks unavailable) · IS-IS 2/2 up
```

The count comes from the first collection pass, but for at least one session the other end could
not be checked live in this run, so the session was not verified from both sides. When a follow-up
check confirms the session from the reachable side, a **Drill:** line says so. An unverified far
end is a gap in evidence, not a sign that the session is down.

### How is BGP peer state read from the device?

From `show bgp summary`. On IOS-XR the `St/PfxRcd` column holds a prefix count for an established
peer (for example `0` or `445667`) and a state word such as `Idle` or `Active` otherwise. A number
is treated as Established.

### What goes under Attention?

- A BGP or IS-IS neighbor that could not be mapped to a device in the NSO inventory.
- A device whose automated live queries timed out or failed. Further live queries to it are skipped
  for the rest of the run. NSO configuration may still be present and manual access may still work,
  so this is not proof that the device is down.

---

## Services

### How do I read the Services table?

```text
| Service type | Total | OpUp | Down | Degraded | Unknown |
```

Each service is counted once, and the four status columns add up to Total.

| Status | Meaning |
|--------|---------|
| **OpUp** | Required PE-side operational checks passed, by the basic checks or a supported LLM conclusion. Customer traffic delivery was not tested |
| **Down** | A required service component or path is confirmed failed |
| **Degraded** | Confirmed partial impairment |
| **Unknown** | The sync prerequisite failed, or operational evidence is insufficient |

In the HTML report, expand a count to see what produced it, for example a basic operational pass
or an LLM conclusion. The "basic operational checks" lines under the table report the deterministic
checks for each service type; they are separate from LLM dataplane investigation.

### Why did only some services get an LLM investigation?

Services whose endpoint devices are in sync and that pass the basic operational checks are not
sent to the LLM; the report gives the number skipped. The remaining services are investigated
within the limits set by `--max-dataplane-per-category` and `--max-dataplane-services`.
Per-service sections appear only for services that were investigated or have a fault; add
`--services-detail` to list every instance.

### Why is a service Unknown when it looks fine?

Unknown describes the investigation, not the service. It means there was not enough evidence:
a query to NSO or the device timed out, a tool returned an error, a device was skipped after a
failed live query, the sync check gave no answer, or the LLM stopped before reaching a conclusion.
None of these alone proves an outage. Down requires positive evidence that a required component or
path failed.

Some NSO deployments return no usable answer from `check_service_sync` for every service. In that
case set `NSO_SERVICE_SYNC_MODE=skip`: the per-service sync calls are skipped and configuration
sync is taken from the endpoint devices' fleet sync instead. The default is `check`.

---

## Metrics

### Is report data in Prometheus?

When `PROMETHEUS_PUSHGATEWAY_URL` is set, each published run pushes count rollups to the
Pushgateway, labelled `pipeline="diagnostic"`. Dry runs do not push, and `--skip-metrics` turns the
push off for one run. Only counts are exported, not the topology itself. See `deploy/monitoring/`.

---

## Related docs

- [Diagnostic runner guide](DIAGNOSTIC_RUNNER.md) — install, run, and read the report
- [Topology design](../nso_facts/topology/docs/DESIGN.md) — design notes for the topology facts layer
- [README.md](../README.md) — setup, environment variables, delivery and scheduling
