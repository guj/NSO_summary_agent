# Changelog

Changes that matter to people who read the reports or run the scans, newest first. Code-only
changes are left out; `git log` has everything. **Report meaning** lists changes to what a
status, count or phrase in the report means, so read it first.

## Unreleased

Nothing yet.

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
