# FAQ — NSO diagnostic report

Common questions about reading the report from `nso-diagnostic-run`. To install and run the
agent, see [DIAGNOSTIC_RUNNER.md](DIAGNOSTIC_RUNNER.md).

## The report

### What sections does the report have?

In order: **Summary**, **Changes since previous run**, **Devices**, **Services**,
**Recommended follow-up**, **Run configuration** and **Run details**. The Summary is written by
the LLM and is omitted with `--skip-llm`. Findings from LLM investigations appear under the
service they concern; the rest of the report is deterministic.

The HTML report adds **Service topology** and **Routing topology**. Only Summary, Services and
Recommended follow-up start expanded; open any other section by clicking its heading or its link
in the navigation bar. The search box filters by service ID, device, type or evidence text.

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

An **Attention** line or list follows when something about that device needs a look. In the HTML
report each device also has a collapsed **Interfaces and hardware** block: interface counts,
hardware readings, and exceptions such as interfaces that are down or could not be matched to the
device. For a device that did not answer live queries, it says the interface state was not
collected instead of listing its interfaces as missing.

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
**Health:** NSO↔device interface mapping unconfirmed; control-plane drop counter=1,234 recorded. Labels: interfaces=Inventory Review; hardware=Review. Review is a triage flag from this run's checks — do not treat it as a confirmed hardware fault (per-device details are in the HTML report).
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

For case 1, an administrator can record the confirmed NSO-to-device name mapping in a JSON file
and point `INTERFACE_EQUIVALENCES_FILE` at it. The schema is shown in
`config/interface-equivalences.example.json`. For case 2, correct the NSO configuration instead.

An interface the device lists but reports no state for, such as a Ciena's management ports, is
not a mapping problem: the device's interface summary counts it under "No state", and it does not
set this label. Nor does a device whose interfaces could not be read at all, because NSO could not
connect to it or it is southbound locked: its health line says the interface status was not
collected, and its interface summary counts them under "Not read".

### Where do names like `HundredGigE0/0/0/9` and `Hu0/0/0/9` come from?

The long form comes from NSO device configuration. The short form is what the device prints in live
`show` output. The agent matches the two with fixed prefix maps (`HundredGigE`↔`Hu`,
`TwentyFiveGigE`↔`TF`, `BVI`↔`BV`, and so on), so both forms can appear in evidence.

### How are the BGP and IS-IS counts calculated?

`BGP 2/2 up` means two sessions between this device and other devices in the NSO inventory, both
up. Neighbors outside the NSO inventory are not counted, so the number can be lower than what
`show bgp summary` lists on the device. They are sorted by the AS number in that table:

- **Outside peers**: routers in an AS that no NSO device uses, such as a research and education
  network. NSO never lists them, so only this device's end is visible. They get their own count
  on the Routing line, `outside BGP peers 3/5 Established; 2 Active (AS 64510)`. One that is not
  Established is listed in the follow-up. One that was Established in the previous scan and is
  not now is listed under Changes since previous run.
- **Neighbors to identify**: in FABRIC's own AS, or with no AS the agent could read, yet no NSO
  device has that address as its router ID. These appear under Attention and in the follow-up:

```text
**Attention:** BGP neighbor matches no NSO device: 192.0.2.1 (AS 64500)
```

`N/A` means no sessions to inventory devices were found for that protocol.

### What does "(initial; peer live checks unavailable)" mean?

```text
**Routing:** BGP 2/2 up (initial; peer live checks unavailable) · IS-IS 2/2 up
```

The count comes from the first collection pass, but for at least one session the other end could
not be checked live in this run, so the session was not verified from both sides. When a follow-up
check confirms the session from the reachable side, a **Drill:** line says so. Under a device that
could not be read, that line names the peer that reported the session. An unverified far
end is a gap in evidence, not a sign that the session is down. When one end did read the
session, the line shows that reading instead (next question).

### What does "IS-IS adjacency down: … lists it as Init; … does not list it" mean?

```text
IS-IS adjacency down: newy-data-sw HundredGigE0/0/0/19.3000 lists it as Init; rutg-data-sw HundredGigE0/0/0/23.3000
does not list it. Ports as read this scan: newy-data-sw HundredGigE0/0/0/19.3000 up; rutg-data-sw
HundredGigE0/0/0/23.3000 link down (not shut down).
```

An IS-IS adjacency works only when both routers hear each other, so anything short of Up at both ends is down.
Both routers answered this scan; the line gives what each one lists. A router showing **Init** still hears the
other's hellos; one that **does not list it** hears nothing usable: either the direction toward it is failing
(fibre, optic, far transmitter) or it rejects the other's hellos, for example over an IS-IS setting that does not
match. The port states help tell these apart. "Both routers answered without it; up in the previous scan" means neither
lists it now. The port states are each end's interface as `show interfaces brief` read it: "link down" is a
lost link, "shut down by configuration" is a deliberate one.

"LLDP: A → B heard (B's port lists A's port); B → A not heard (A's port lists no LLDP neighbor)" comes from both
ends' `show lldp neighbors`, read in the same scan. An LLDP entry on a router records frames that router received,
so each direction is told by the receiving end's table: B listing A shows that A → B works, and says nothing about
B → A. "Not heard" is a reading: frames from that side are not arriving, or are not sent. "Lists … instead" names
another router heard on that port, as a fibre patched to the wrong place would show. When a router does not list
the adjacency, its port is taken from the other end's LLDP ("named by …'s LLDP"), so its optics can still be read.

When the line goes on with "Optics: A → B: transmitted …, received …, loss … dB per lane; B → A: …", both ends'
`show controllers <port>` were read in the same scan: each direction is one end's transmit power against the
other end's receive power, lane by lane. A direction losing several dB more than the other points at that
direction's fibre, connectors or optics, even while the receive level is still inside the alarm thresholds. The
numbers are readings only; the report draws no conclusion from them. "Not read" means that end's output could not
be read or parsed (for example a platform without that output).

### What do "Established here, far end not read" and "not read on <device>" mean?

```text
**Routing:** BGP 17/21 up; 3 Established here, far end not read (brist-data-sw, cien-data-sw, prin-data-sw); 1 Idle (cape-data-sw) · IS-IS 10/10 up
**Routing:** BGP not read on prin-data-sw; its peers report 2 of 2 Established (star-data-sw, wash-data-sw) · IS-IS not read on prin-data-sw; its neighbors report 1 of 1 up (newy-data-sw)
```

`up` counts only sessions confirmed from both ends. When the device at one end could not be read
this run, the line shows what the other end reported instead of leaving the session uncounted:
on the device that was read, as "Established here, far end not read" or the state it saw, such as
`Idle`; on the device that could not be read, as what its peers or IS-IS neighbors report. An
IS-IS adjacency toward a device that could not be read keeps that device's interface as `?`,
because only that device can name it.

"IS-IS not readable on cien-data-sw (its NED exposes no adjacency state)" is a limit of the
platform, not a failed read: the Ciena SAOS NED models IS-IS configuration only, so no scan can
read adjacencies from that end. Its neighbors' view is the only reading of those adjacencies.

### How is BGP peer state read from the device?

Through the MCP server's `get_bgp_summary`, which runs `show bgp ipv4 unicast summary` on IOS-XR.
The agent reads the table as the device printed it. The `St/PfxRcd` column holds a prefix count
for an established peer (for example `0` or `445667`) and a state word such as `Idle` or `Active`
otherwise. A number is treated as Established.

On Ciena SAOS the NED (`ciena-saos_nc-gen-1.0`) models BGP configuration but no session state, so
the device line says "BGP not readable on cien-data-sw (its NED exposes no session state)" and
its sessions are read from the peers' end only. This is a fact about that NED version: a newer
one could expose session state, and the agent would then use the server's per-neighbor rows.

### What goes under Attention?

- A BGP or IS-IS neighbor that could not be mapped to a device in the NSO inventory.
- A BGP session to another inventory device that the devices report down. It is also counted in
  the Result line and listed in the follow-up. **Changes since previous run** lists BGP sessions
  and IS-IS adjacencies that were up in the previous run and are not up now, and interfaces that
  went from up to down. An interface is marked "its service was deleted" when every service the
  previous scan had on it has since been deleted by its owner; that is not a fault on its own.
  It is marked "already Down in the previous run" when that scan read the interface up and then
  found its services Down: the port failed during that scan, which already reported the loss.
  It also lists the service faults still open, each with the scan that first
  listed it and, for a down link, how long the device says it has been down. With
  `NOTIFICATION_DIGEST=1`, the Slack and email digest starts with the same changes.
- A device whose automated live queries timed out or failed. Further live queries to it are skipped
  for the rest of the run. NSO configuration may still be present and manual access may still work,
  so this is not proof that the device is down. A single read that ran out of time while the
  device was still printing its answer does not count: only that read fails, and later reads
  still go to the device. If NSO itself stopped answering, the scan stops
  instead and produces no report; see
  [When NSO stops answering during a scan](DIAGNOSTIC_RUNNER.md#when-nso-stops-answering-during-a-scan).

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

### Which command did a Collector detail line come from?

In the HTML report, click the line. Each `Basic check …` line under **Collector detail** opens to
the `show` command or commands its verdict was read from, with the device and the time of the read;
"(no output)" marks a command that returned nothing. A line with nothing to open was decided from
the service's own definition, not from a command. The command is shown, not its output; run it
again on the device, or keep the outputs with `--save-mcp-results`.

### Why did only some services get an LLM investigation?

Services whose endpoint devices are in sync and that pass the basic operational checks are not
sent to the LLM; the report gives the number skipped. The remaining services are investigated
within the limits set by `--max-dataplane-per-category` and `--max-dataplane-services`.
Per-service sections appear only for services that were investigated or have a fault; add
`--services-detail` to list every instance.

A down port is investigated once, before the services. It explains a service when the
service's basic check is Down and every failed check of that service is on a link recorded as
down, provided the investigation itself reads that link down. Such a service is not investigated
individually, and the port's finding is printed under its device. Its status depends on what is
still up:

- **Down** when what is left cannot carry traffic: a side of the service has no attachment up, or
  fewer than two of its ends are up in all. The table shows "Down — port investigated once".
- **Degraded** when an attachment is up on every side and two ends are up in all. The table shows
  "Degraded — port investigated once". The admins chose this on 14 October 2026.

The sides come from the service's configuration; a site-to-site service has two, so one with
nothing up at one site is Down however many are up at the other. For a routed service a working
gateway counts as an end, so one with a single attachment left and its gateway up is Degraded. A
port that leaves some services Down and others Degraded is listed once for each. A BVI, Loopback or Null
interface is never investigated as a port: it has no port of its own, and a BVI is down because
its member port is down. A bundle is investigated like any other port. A port investigation that
does not finish explains nothing, and its services are investigated as usual.
`--max-port-investigations` sets the ceiling (default 6).

A cut-off device is handled the same way. A device is cut off when it answered the scan's
routing queries and has no IS-IS adjacency and no BGP session up. It is investigated once, and
its services that do not pass their basic check and whose own configuration names another
device are reported Down with "Down — device investigated once". Services on that device whose
configuration names no other device keep the result of their own basic check.

A duplicate gateway needs no investigation at all. When two l3rt services on one device are
configured with the same gateway address, the device gives the subnet's route to one of them.
The service whose route is bound to the other's interface is reported Down by the basic check
itself, with "Down — decided by the fixed check", and its section names the service that holds
the address. The holder stays Up and its section notes the shared address. If the device's route
cannot be read, the shared configuration alone decides nothing and the service is investigated as
usual. Only l3rt services have a gateway address, so no other type is affected.

A port mirror the device itself calls not operational needs no investigation either. The basic
check reads `show monitor-session status detail`; when a source of the service's session is shown
as "Not operational (reason)", for example because the source port is also the destination of
another session, the service is reported Down with "Down — decided by the fixed check" and the
device's reason. If the source port itself is down, the port is still investigated once, like any
other down port.

### Why is a service Unknown when it looks fine?

Unknown describes the investigation, not the service. It means there was not enough evidence:
a query to NSO or the device timed out, a tool returned an error, a device was skipped after a
failed live query, the sync check gave no answer, or the LLM stopped before reaching a conclusion.
None of these alone proves an outage. Down requires positive evidence that a required component or
path failed.

A service type the agent has no checks for is also Unknown. For those it only looks at the
interfaces the service names: if one is down the service is reported Down; if all are up it stays
Unknown with the reason "Interfaces up — service logic not checked", because working ports say
nothing about whether the service itself works. Types with their own checks are l2ptp, l2bridge,
l2sts, l3rt and port-mirror.

A service also stays Unknown when its basic check asked a device and got no usable answer to any
command, even if the LLM then read the device another way and concluded Up. With nothing from the
basic check, Up would rest on the model's judgement alone, and that judgement has gone both ways
on the same readings. The LLM's finding is kept in the service's section. A fault the LLM finds
on such a device is still reported Down.

**Changes since previous run** also lists "Links gaining errors while up": ports under a service or
an IS-IS adjacency that are up and whose CRC, input-error or output-error count grew since the
previous scan, or that went down and came back. It is an early sign of a failing optic or fibre,
reported as a lead; it does not change any service's status.

**Changes since previous run** lists a service under "Status changed while the fixed checks read
the same" when it was investigated in both scans, its basic check read exactly the same, and its
final status differs. That is a change of verdict, not a change on the network.

Some NSO deployments return no usable answer from `check_service_sync` for every service. In that
case set `NSO_SERVICE_SYNC_MODE=skip`: the per-service sync calls are skipped and configuration
sync is taken from the endpoint devices' fleet sync instead. The default is `check`.

---

## Metrics

### Is report data in Prometheus?

When `PROMETHEUS_PUSHGATEWAY_URL` is set, each published run pushes count rollups to the
Pushgateway, labelled `pipeline="diagnostic"`. Dry runs do not push, and `--skip-metrics` turns the
push off for one run. Only counts are exported, not the topology itself. See `deploy/monitoring/`.

The counts include the report's final status per service type (`nso_services`), services that are
not OpUp per device (`nso_service_faults`), and how many devices could not be covered. Links that
gained errors while up since the previous scan add CRC errors per minute (`nso_link_crc_per_minute`)
and down/up changes (`nso_link_state_changes`); a rate is the average over the gap between the two
scans, a lead rather than a service status. A scan that
ends without a report, for example because NSO became unreachable, pushes only a failed-attempt
signal (`nso_scan_last_attempt_success` 0) and leaves the last good scan's values in place. The
**NSO Diagnostic** Grafana dashboard charts these.

---

## Related docs

- [Diagnostic runner guide](DIAGNOSTIC_RUNNER.md) — install, run, and read the report
- [Topology design](../nso_facts/topology/docs/DESIGN.md) — design notes for the topology facts layer
- [README.md](../README.md) — setup, environment variables, delivery and scheduling
