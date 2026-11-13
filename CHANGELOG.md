# Changelog

Changes that matter to people who read the reports or run the scans, newest first. Code-only
changes are left out; `git log` has everything. **Report meaning** lists changes to what a
status, count or phrase in the report means, so read it first.

## Unreleased

### Report meaning

- **Links gaining errors:** for a port with no CLI (a Ciena device read through `get_interface_health`), "went down and came
  back since the previous scan" is now decided by comparing the device's own last-change stamp in the two scans, not
  the stamp with the scan host's clock, so a device clock that is fast or slow can neither invent nor hide it. It is
  also not said of a port that was already down at the previous scan. No scan has read such a port since 27 Oct, when
  NSO lost its connection to cien-data-sw.
- **Services, sync and final status:** a Down or Degraded finding now counts when a device behind the service is
  out of sync, locked, errored or was not read. Before, any such device held the service at Unknown, so a
  completed investigation's Down (a named `--service`, or a port or device investigation) was discarded. The
  reason carries the sync state, for example "LLM concluded Down, sync out". OpUp is still held at Unknown there,
  since a pass without a trusted configuration baseline claims too much. The "left Unknown" groupings and the
  Sync verification failed group count only services whose final status is Unknown. Replayed over the last 25
  saved scans, the Services line is unchanged: none had an investigation behind a failed sync.
- **Dashboard and metrics, devices not in sync:** `nso_fleet_devices_out_of_sync` now counts only devices
  NSO reports as out of sync (configuration drift). A southbound-locked device, which NSO's summary
  counted as out of sync, is exported as the new `nso_fleet_devices_locked`. On 12 Nov the one
  "out of sync" device was brist-data-sw, locked, with no drift anywhere. The dashboard's single
  "Devices not in sync" tile is three: Config drift (orange), NSO can't reach (the sync errors, red)
  and Locked (blue). Locked shows no data until a scan runs on the newer image.
- **Services:** a service reported Down or Degraded that its owner deleted before the scan ended is
  no longer counted or listed as a fault. At the end of the scan, one fresh NSO listing per service
  type that has such a service is taken; a service that is no longer listed appears under "Services
  no longer present" with the other deleted services, and the next scan's changes list it too. Until
  now a deleted Down service stayed an open fault until the next full scan. A listing that cannot be
  read keeps the fault, and services reported Up or Unknown are not rechecked.
- **Changes since previous run:** a port that a scan read down and then found up again when it
  read the port's counters is listed once, under "Interfaces up in the previous run, down now",
  marked "up again later in this scan" with its state changes since the previous scan. It used to
  be listed under "Links gaining errors while up" as well, which said no service status rested on
  it although its services could be Down or Degraded because of the flapping.
- **Changes since previous run:** "up again later in this scan" also marks a sub-interface whose port
  the later counter read found up after changing state since the previous scan, naming that port
  ("Bundle-Ether101 up again later in this scan: …"), and a bundle member the later member read
  found Active. On 10 Nov dall-data-sw's Bundle-Ether101.3000 and its members were left unmarked,
  and its bundle was listed again under "Links gaining errors while up".
- **Investigated by LLM on (day):** an earlier scan's finding on an IS-IS adjacency or BGP session
  says "investigated again this scan, no conclusion" when this scan's investigation read it again
  but ended without a conclusion. "Not rechecked this scan" now means it was not read again. On
  10 Nov the wash-data-sw ↔ cien-data-sw investigation was stopped after three refused conclusions,
  and the device blocks said the earlier finding was not rechecked.

- **LLM investigations** (new Result line): how the scan's LLM work went. **Complete**: every
  investigation the LLM started reached a conclusion and it wrote the Summary. **Partial**: some did
  not, counted with the reason. **Unavailable**: no LLM request succeeded. **Skipped**: the LLM was
  not used. **Halted**: the provider's spend limit was reached. Service, routing and port
  investigations are counted together; a conclusion is still "not verified". Slack and email carry
  the line unless it is Complete ([docs/FAQ.md](docs/FAQ.md), "What does the 'LLM investigations'
  line in the Result mean?").
- A service the fixed check found Down or Degraded, whose LLM investigation did not finish, says so
  in its own block ("Down by the fixed check — the LLM investigation did not finish") and in the
  summary written without the LLM. It was already counted Down, but its block and that summary said
  "dataplane=unknown; unresolved", "Unknown category" and "Service forwarding status remains
  unknown".

### Added

- **Links gaining errors while up:** each line now ends with the light at both ends of the link
  ("Optics, each end on its own: …"), read in the same scan with `show controllers <port>`, so "CRC rising" comes
  with a reading. The ports are chosen from this scan's counters and the previous scan's, after the adjacencies
  down and under the same 20-read limit; the run details' Optics reads line says how many were for them. A
  bundle shows its members and their state. A breakout lane such as `HundredGigE0/0/0/64/3` or toky's
  `TenGigE0/0/0/18/0` is read from its parent port's `show controllers optics`, one read for all its lanes, and shows
  its own lane with the receive low-alarm threshold. On the last two scans that is 7 reads. The parent-port layout
  was taken from star's and fiu's replies; toky's own has not been seen, and an unreadable reply is "not read".
- `nso_device_cpu_five_min_percent` and `nso_device_memory_used_percent` give each device's 5-minute CPU and memory
  use (a `device` label; none for a device that could not be read). The dashboard's hardware row shows the busiest
  and the fullest device, with its name, instead of a count of devices over the alert line. Temperature, fans and
  power supplies stay counts.
- `llm_exchanges.json` in each saved run folder keeps every LLM investigation's conversation: the request, each
  tool call and the device's reply as the model saw it, and its conclusion, with each system prompt stored once.
  It is the material for replaying a changed prompt against a past scan (a later step). It is local like
  `case.json`, not published, and is removed with the rest of the run when old scans are pruned. It is
  written only when the scan had an LLM investigation.
- `nso_scan_last_attempt_state` (0 ok, 1 failed, 2 NSO unreachable) says in one value how the latest scan attempt
  ended, as `nso_llm_state` does for the LLM work. The two 0/1 gauges stay. The dashboard's latest-attempt banner
  will read it instead of working the state out in its query.
- A fixed check that did not pass shows what the device answered. Under **Collector detail** in the HTML
  report, a `Basic check` line of a fault or unknown verdict opens to its command and, below it, an
  excerpt of the reply (up to about 2,500 characters, and the part the verdict was read from: a
  bridge-domain's header and members, a port-mirror service's own session, a forwarding reply's line for
  the port, or the lines around the one the check read its verdict from), or the reason a read failed, such as "RESTCONF 500: Device brist-data-sw is southbound locked". It was
  the command alone, so "gateway_identity unknown" for net1 could not be traced to what newy-data-sw
  returned ([docs/FAQ.md](docs/FAQ.md), "Which command did a Collector detail line come from, and what
  did it return?"). A line above the commands says what the check looked for, and a line under it
  what the check found instead ("no BVI among the members …", "2 BVIs among the members …", "% Network
  not in table"), so a verdict that failed because a line was missing or wrong can be read too.
- A scan pushes how its LLM work went and what it cost: `nso_llm_state` (complete, partial, unavailable,
  skipped or halted, as in the report's "LLM investigations" line), investigations done and total,
  requests and tokens, and, when the provider reports them, `nso_llm_cost_usd` and the budget left,
  with the model's name. The **NSO Diagnostic** dashboard has an LLM row for them. The provider's own
  messages are never pushed ([docs/FAQ.md](docs/FAQ.md), "Is report data in Prometheus?").
- [docs/SERVICE_EXAMPLES.md](docs/SERVICE_EXAMPLES.md): one real service per type (l2bridge, l2sts,
  l3rt, l2ptp) followed from NSO's intent through the checks to its final status.
- `faults.json` lists `no_longer_present`: service faults open when last read whose service a full
  scan no longer finds in NSO, because its owner deleted it. They used to appear in no list, so a
  ticketing script had no way to close them.

### Changed

- `faults.json` is format version 2. Its `scope` says what the scan examined: `kind` is `full` or
  `partial` (it was `full` or `services`, and a scan narrowed by `--devices` or a layer option was
  wrongly `full`), with new `checked`, `devices` and `ignored_service_types` fields
  ([docs/DIAGNOSTIC_RUNNER.md](docs/DIAGNOSTIC_RUNNER.md), "faults.json: the scan's faults for a
  script").
- The summary gives an LLM investigation's cause for an IS-IS or BGP fault as that investigation's
  conclusion, not verified, as the device's block already did; it used to state it as fact.
- A port investigation with no power reading to go on (a copper cable, or optics not read) says
  its readings cannot tell which end or the cable is at fault. It used to place the fault beyond
  the port, and its suggested next step read as if the port's configuration had been ruled out.
- **Links gaining errors while up** names the services on a port that carries three or fewer, for
  example "carries 1 service (`l2bridge/link-h4-tofino-…`)". It used to give only the count, so a
  service listed as no longer failing could not be matched with the flapping port it rides on.
- An LLM investigation whose conclusion is refused three times in a row stops there; a service
  dig stopped this way is Unknown, "refused 3 times in a row: no usable cause". The model is now
  told which field was refused and what it held. On 9 Nov a drill re-sent a conclusion without a
  usable cause 13 times, until its round limit, told each time only that one was needed.
- A drill read that the device itself rejected (a node it doesn't have, invalid input) is to be
  reported as a failed command and corrected, not as a timeout. Ten salt ↔ seat drills asked for
  salt's NPU counters at a location salt doesn't have, reported a timeout, and told operators to
  collect the counters by hand with the same command.
- LLM investigations are told that a hardware-health read includes the NPU's trap counters, totals
  for the whole NPU rather than per port. On 10 Nov the salt ↔ seat drill held salt's trap counters
  from its own read, called it a sensor reading, and told operators to collect the counters by hand
  because they were "not obtainable via MCP".
- Drills no longer go to collection errors or to a device with no IS-IS or BGP in its NSO
  configuration: the issue already gives the error, and a drill's finding on them appeared in no
  device block. After the 10 Nov scans with `--max-drill-issues 20` had investigated every IS-IS and
  BGP issue, these would have taken both drill slots in every later scan at the default of 2.
- An l3rt investigation whose gateway is a BVI reads both the bridge-domain of the service's port and
  the one the BVI is bound to, and names both when they differ. On 10 Nov one read only the port's
  bridge-domain and suggested adding the service's gateway to another service's bridge-domain; the
  scan before had found the gateway in the service's own bridge-domain, with no port in it.
- A device block's "Next:" after an LLM investigation of an IS-IS adjacency or BGP session is the
  investigation's first step, which it now gives in one sentence; the full suggested fix stays in
  the HTML report. It used to be the opening of the full fix, and on 10 Nov six of eight showed only
  a sentence such as "No config change is indicated", leaving out the steps that followed: a
  shut/no-shut needing approval and a maintenance window on salt-data-sw, and restoring NSO's
  connection to cape-data-sw first. Findings from earlier scans still show the fix's opening.
- An LLM investigation of an IS-IS adjacency or BGP session is told the platform of an end whose
  platform exposes no IS-IS or BGP state (a Ciena on SAOS), and gives commands and configuration
  only in a platform's own syntax, describing the check otherwise. On 10 Nov the star-data-sw →
  cape-data-sw investigation gave IOS XR commands and `router isis` configuration for cape-data-sw,
  as earlier ones had for cien-data-sw.
- An LLM investigation of an IS-IS adjacency or BGP session says a device "could not be read"
  where it used to call it "quarantined", the agent's own word for a device a scan stops reading
  after a failed read, and no longer names its input fields. On 10 Nov two findings' "Next:" began
  "Get cien-data-sw out of quarantine", which is nothing an admin can do.
- A failed LLM request is shown by the kind of error ("provider connection error", "provider timed
  out"), never by the provider's own text, which stays in the scan's log. The summary-failure line,
  the Run details notes and a service's block used to print that text, and with publishing on it
  went to Slack and email: on 4 Nov a model server's address and port, and for a spend-limit halt
  the provider's message naming the user and the amounts.
- A port or device investigation that did not finish says why when a request failed or its
  conclusion was refused ("investigation did not finish: the LLM request failed (provider
  connection error)"), and "its 1 service was left to its own investigation" no longer reads
  "were … their".
- An LLM investigation's "Next:" for an IS-IS adjacency or BGP session ends "(human must approve any
  config change)" when the investigation flagged its fix as needing approval and the step does not
  already say so, as a service's Next action does, including on a later scan's "Investigated by LLM
  on (day)" line. Findings from scans before this change have no marker. On 10 Nov the salt-data-sw
  ↔ seat-data-sw "Next:" was a shut/no-shut on salt-data-sw with no word on approval.
- An LLM investigation is no longer stopped by a conclusion whose fields ran together: when the model
  closes one field with the wrong tag, the provider puts the next fields inside it (``…</observed>
  <parameter name="cause">…``), and the conclusion was refused as having no cause. The scan now splits
  them back out and logs the repair. This was behind every "no usable cause" refusal since 9 Nov; on
  11 Nov the amst-data-sw ↔ brist-data-sw investigation was stopped after three such refusals.
- When the services chosen for an LLM investigation include some that owners have deleted since the
  scan listed them, the next ranked services take their places, so the cap (`--max-dataplane-services`,
  default 10) is filled when enough remain. The deleted ones still appear under "Services no longer
  present". On 11 Nov six of ten chosen l2bridge services were deleted before their turn and their
  slots stayed empty, while seven fiu-data-sw l2bridge services were left Unknown "not investigated".
- **Services no longer present since the previous run** marks a deleted service that was an open
  fault, for example "— Down in the previous run", and lists those first; a cut-off list counts the
  faults it leaves out. The fault ended with the deletion, not a recovery. The summary's input marks
  them too. On 9 Nov three Down services were deleted and the report gave only their names, while
  the summary said service status had not changed.
- An IS-IS adjacency down whose ports the interface read found down and the later counter read found
  up says so in the Result and its follow-up ("both ends' ports up when read later, at 00:56 UTC;
  the adjacency was not re-read"), and its Attention line gives the later reading beside the
  earlier one. It is still counted down and listed in `faults.json`. On 10 Nov dall ↔ losa was
  counted down while the summary said it had recovered, and its Attention line set "link down"
  beside "3 of 3 members active", read half an hour apart, without saying so.

### Fixed

- LLM investigations get the readings of an interface-health or hardware-health read. They used to
  get only the platform name ("ios-xr" or "saos"), and a dig's saved evidence kept only that: 213
  reads in 51 of the last 53 scans. On 10 Nov the salt ↔ seat drill said its hardware read gave "no
  fault detail", though that read holds salt's NPU trap counters.

## 0.2.0 (November 2026)

The first version with a changelog. Earlier history (0.1.x, from August 2026) is in `git log`.

### Report meaning

What the report's terms mean as of this version; later entries say how they change.

- **OpUp:** the required PE-side checks passed. Customer traffic delivery was not tested.
- **Down:** a required service component or path is confirmed failed, on positive evidence. A
  timeout, query error, missing mapping or unreadable device is not enough on its own.
- **Degraded:** confirmed partial impairment.
- **Unknown:** too little evidence: the sync prerequisite failed, or the operational evidence is
  insufficient. Unknown is not an outage, and `faults.json` only counts it.
- **New this scan / newly detected:** not open in the previous scan. Not a proven regression:
  the previous scan may have had it Unknown or not had it at all, so the onset is unverified.
- **First seen / Open since 27 Oct:** the earliest saved scan from which it has been open in every
  scan that read it: when a scan first saw it, not when it started. **"or earlier"** means the
  saved scans do not reach back to its start.
- **Not rechecked:** this scan did not read it, so its current state is unknown; it is not
  recovered. **Recovered** means read clear after last being seen open.
- **Services no longer present:** gone from NSO since the previous scan, normally deleted by their
  owners; not a fault.

### Added

- `--profile FILE` keeps a scan's options in a file
  ([docs/DIAGNOSTIC_RUNNER.md](docs/DIAGNOSTIC_RUNNER.md), "Saving options in a profile").
- New / not-new labels. With an earlier scan to compare with, each Result count is tagged
  (none new), (not new), (N new: …) or (not read last scan), and each follow-up item starts with
  New this scan, Open since (day), Carried over or Not read last scan
  ([docs/FAQ.md](docs/FAQ.md), "How do I tell what is new this scan?").
- A newly failed service in **Changes since previous run** says what the previous scan's fixed
  check passed on its circuits.
- Devices NSO failed to log in to (for example `SSH host key mismatch`) are named in the Result,
  with a follow-up to verify the host keys out of band ([docs/FAQ.md](docs/FAQ.md), "What does
  'NSO failed to authenticate' mean?").
- A publishing scan that stops without a report sends a one-line notice to the report's Slack and
  email. An opt-in Grafana alert fires when published scans stop arriving
  ([deploy/monitoring/README.md](deploy/monitoring/README.md), "Alert when scans stop").
- `faults.json` in each saved run folder: the scan's faults as new, still open, recovered or not
  rechecked, for a script such as a ticketing tool. Format version 1
  ([docs/DIAGNOSTIC_RUNNER.md](docs/DIAGNOSTIC_RUNNER.md), "faults.json: the scan's faults for a
  script").
- [docs/SLACK.md](docs/SLACK.md): setting up Slack delivery.

### Changed

- The Result is a list with one line per kind of problem, instead of a single line.
- The default email subject starts with "NSO Diagnostic" instead of "NSO Summary";
  `EMAIL_SUBJECT_PREFIX` still overrides it. Metric names stay `nso_summary_*`
  ([README.md](README.md), "Names you will see").
- `--dataplane-concurrent-workers` is the option's spelling; the old `--dataplane-concurrent_works`
  still works.
- The package is now `nso-diagnostic-agent`. A `.venv` from before November 2026 still has the old
  `nso-summary-agent`; `install.py` removes it ([docs/DIAGNOSTIC_RUNNER.md](docs/DIAGNOSTIC_RUNNER.md)).

### Removed

- The older runners: `nso-diagnostic-run` is the only one.

### Fixed

- When a device's IS-IS read fails and a later read answers, the later answer now decides the
  adjacency state.
- A routing link missing from the configuration read this scan (as when a device could not be
  read) is no longer dropped from the topology: its ends are read from the link's id, or a
  `[topology] could not name the ends of …` warning names it.
