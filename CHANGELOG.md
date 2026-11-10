# Changelog

Changes that matter to people who read the reports or run the scans, newest first. Code-only
changes are left out; `git log` has everything. **Report meaning** lists changes to what a
status, count or phrase in the report means, so read it first.

## Unreleased

### Report meaning

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

### Added

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
