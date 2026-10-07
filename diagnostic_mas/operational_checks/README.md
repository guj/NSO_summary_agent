# Basic operational checks

Enabled by the diagnostic runner in the concurrent copy. Legacy summary and
multi-agent callers retain their existing behavior. Every check uses this run's
read-only evidence; no device configuration is changed.

## Flow

1. Require every intended endpoint/device in-sync and no negative service-sync
   result. The existing null-service-sync endpoint fallback remains explicit.
2. Run the service module. A pass is **Operational Up / PE-side readiness**,
   not verified customer delivery or an LLM dataplane pass.
3. A positive fault is Down; unavailable, unsupported, ambiguous or incomplete
   evidence is Unknown. Those results enter the existing bounded LLM selection.
   Sampling limits still apply: eligibility is not a guarantee of investigation.
4. `--service-id` explicitly permits deeper investigation, including basic passes
   and sync-incomplete instances. `--skip-llm` prevents LLM calls.

## Modules and pass criteria

- `common.py`: evaluates collected sync evidence; no duplicate sync queries.
- `l2ptp.py`: every intended AC-matched xconnect UP, no contradictory segment state.
- `l2bridge.py`: exact observed bridge identity, all intended ACs UP and members,
  programmed local forwarding members and flood readiness. Remote/routed or
  non-default switching dependencies go to investigation rather than falsely pass.
- `l2sts.py`: bridge checks plus scoped effective RT import/export compatibility,
  each PE direction's remote replication label matching the receiving PE, and
  resolved labeled transport. Explicit-config absence is never an RT fault;
  empty MAC tables and zero pseudowires are not outages. Unsupported alternative
  forwarding proofs remain Unknown for the LLM to investigate.
- `l3rt.py`: exact gateway binding from bridge evidence, attachment/gateway UP,
  configured family addresses, service-specific RIB/CEF entries on the local PE
  and all configured border routers. External services include default routes.
  Missing family/role/VRF identity or unsupported direct-routed variants remain
  Unknown. No customer address, BVI or VRF is invented.
- `port_mirror.py`: one `monitor-session status` read per device, shared by its
  services. The session is matched by the service's destination interface in
  the device's answer, never by a name built from the service ID. Pass needs the
  session, every intended source operational in the intended direction, and the
  destination and source interfaces up. A session missing from the answer is
  Unknown, not Down: the service may simply be gone. The status layout follows
  Cisco's documentation and has not yet been sampled from a live device.
- `generic.py`: every other service type. It reads the state of each interface
  the instance names (the shared `type`/`id` shape). An interface down is Down;
  otherwise the result is Unknown with "only its interfaces were examined". It
  never reports Up, and such types are not sent to the LLM because they have
  no prompt of their own.
- `probe.py`, `bridge.py`: conservative IOS-XR output adapters and bounded collection.
  Other output formats fail closed to Unknown. Device commands are sequential by default;
  identical queries (including errors) reuse a run-local cache. No automatic retry
  or CLI syntax guessing. Non-L2PTP basic collection is capped at 24 cache misses
  per service. Existing MCP timeouts and access guards still apply.

## Evidence and reporting

Each service's `basic_checks` stores normalized checks, time, service/device
identity, intent, tool/command provenance, operational result and investigation
reason inside case spine evidence. The owning case supplies the run identifier.
Normal persistence/dry-run rules apply; raw full output archival remains opt-in.
Both dataplane and drill prompts receive these observations; LLM conclusions
remain separate. The report lists basic operational counts per type and includes
checks in service detail. Sync and LLM dataplane table counts remain independent.

Offline tests cover healthy paths, missing identities, RT mismatch, reverse-path
gaps, both address families, border coverage, sync gating, cache reuse and budgets.
Saved XR output fixtures exercise actual parser formats. Live effectiveness still
requires a scoped run; these tests do not establish customer delivery.

## nso40 follow-up

Bridge/interface discovery now attempts one run-local `interfaces brief` and
`l2vpn bridge-domain detail` snapshot per device, reusing scoped matches for
all services. Ambiguous matches stay Unknown; unsupported bulk queries fall
back to the existing scoped reads. Forwarding and routing remain service-scoped
and cached by exact device/command. No previous-run observations are reused.

L3RT does not impose local bridge flooding/split-horizon requirements on routed
gateway attachments. It still requires attachment membership, encapsulation,
forwarding membership, gateway state/binding, and every required RIB/CEF check.
Host destinations use longest-prefix-match lookups without /32 or /128; IPv6
uses `route [vrf NAME] ipv6 ...`. Implicit-null labels are accepted only with
an installed CEF path and positive adjacency evidence, not merely an EVPN TEPid.

Regression tests include nso40 output and cache call-count assertions. Wall-clock
improvement remains unmeasured until a scoped live run. Provider availability and
LLM conclusion gates are independent of these collection fixes.

## Optional spine concurrency

`--spine-concurrent-devices N` defaults to 1. Start with 4 to overlap service
operational collection across different devices. This is separate from
`--dataplane-concurrent_works`, which controls LLM investigations.

Only service operational checks (including L2PTP xconnect probes) use this
option. NSO inventory/sync and other spine phases retain their existing order.
A run-local semaphore bounds active calls, and per-device locks allow only one
live command per device. Groups are processed by primary device, but secondary
endpoint calls use the same locks and global allowance. Per-service dependencies
remain ordered. The underlying MCP client still enforces quarantine/timeouts.

Simultaneous identical observations coalesce under a per-query lock; one caller
collects the result and others reuse it, including failed observations. Each
service retains its own checks and provenance. A previous service passing does
not imply another service passes. Cache scope is the current collection only.

The report records Concurrent spine device calls. Concurrency reduces waiting,
not the number of distinct required queries; actual performance depends on NSO.
160 focused offline tests passed when this option was added. No live restart
was performed automatically.

## Final service summary

The report now shows Service type, Total, OpUp, Down, Degraded, and Unknown.
Each service contributes once, so the four status counts sum to Total.
Sync out or unknown stops routine verification and counts as Unknown.
A supported completed LLM assessment supersedes the basic operational result.
An incomplete or skipped LLM investigation retains the operational result,
including an established Down or Degraded result. Sync alone never proves OpUp.
OpUp denotes PE-side readiness, not tested customer traffic delivery.
In HTML, expand a nonzero status count to see contributing assessment paths.
Historical saved reports are not rewritten.

### Reusing basic replication evidence in the diagnostic gate

The gate credits service-matched basic endpoint and effective EVPN evidence.
For a missing learned-MAC rejection, complete basic attachment, bridge, flooding,
RT and directional replication checks can establish replication readiness.
A transport Unknown must be closed by an exact remote-prefix CEF result; a later
contradictory result blocks acceptance. Missing local or directional checks and
positive basic faults do not qualify for this exception. Accepted findings state
that learned-unicast MAC forwarding and customer delivery were not verified.
The labeled CEF parser supports Bundle-Ether egress names.

### Service disappearance during collection

When a dig concludes that service objects are missing, the runner performs a
fresh get_services query for that service type, bypassing the run cache. Only a
successful complete listing with the exact ID absent confirms disappearance.
Failed, malformed or partial responses retain Unknown. A present ID keeps the
original diagnostic result. Confirmed disappearances are saved with observation
and recheck timestamps, excluded from active-service totals, and reported in
Services no longer present. They are not evidence of intentional deletion or
recovery; consult the service owner/change history before restoring anything.

### Failed device-sync checks

An explicit MCP result=error/unknown overrides an accompanying in_sync=false:
it is an inconclusive check, not confirmed drift. One targeted check_device_sync
retry is retained. If still inconclusive, affected services remain Unknown and
skip routine operational checks and LLM digs. The report groups the distinct
affected services under Sync verification failed and provides one read-only
NSO CLI check-sync command per failing device. Endpoint counts are not summed:
a service touching multiple failed devices contributes once. Raw retry responses
remain in the audit; no sync-from or automatic remediation is performed.
