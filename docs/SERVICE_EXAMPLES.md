# How a service gets its status: one example per type

For each service type the agent checks, one real service from the scan of 6 November 2026
(run `20261106T024352Z`), from what NSO says the service should be to the status the report
gives it. The point is to explain a status without reading the code.

- Device, interface, bridge and label values are as the scan saw them. Service names are
  shortened as in the reports, and public IPv6 prefixes are replaced with documentation
  prefixes (`2001:db8::`).
- A scan keeps the commands each check ran and what the check recorded, not the full device
  output, so the evidence below is those records. Commands are run read-only through NSO's
  `exec_show`, so `interfaces brief` is `show interfaces brief` on the device.
- The exact pass rules for each type are in
  [diagnostic_mas/operational_checks/README.md](../diagnostic_mas/operational_checks/README.md).
  port-mirror has a module too, but the FABRIC admins chose on 14 Oct 2026 not to monitor it
  (`IGNORE_SERVICE_TYPES`), so it has no example here.

| Type | Service | Fixed check | Further investigation | Final status |
| --- | --- | --- | --- | --- |
| l2bridge | `ms-4357c25d…` | Down | Its port, once | **Down** |
| l2sts | `L2STS_DALL_TACC-4bdfe445…` | OpUp | Not needed | **OpUp** |
| l3rt | `net_v6ext-cac0f9da…` | Unknown | LLM investigation | **OpUp** |
| l2ptp | `ATLA_SALT_ERO_NE-3a92562f…` | OpUp | Not needed | **OpUp** |
| l3rt | `inference-fabnet-5d839fd7…` | Not run (sync) | None | **Unknown** |

## How the status is decided, for every type

1. **Sync first.** Every device the service uses must be in sync in NSO. If one is out of sync,
   or its sync state could not be read, the fixed checks do not run and the service is
   **Unknown**.
2. **Fixed checks.** The type's module reads each device and records every check as pass,
   fault or unknown. Any fault makes the service **Down**; every check passing makes it
   **OpUp**; anything else is **Unknown**. A command that answers is not by itself a pass:
   the answer has to show the state the check needs.
3. **Further investigation.** A Down or Unknown service may be looked at again: once per port
   or device when several services share a dark port or an unreadable device, or on its own by
   the LLM, within the scan's limits ([FAQ](FAQ.md#why-did-only-some-services-get-an-llm-investigation)).
   A completed LLM conclusion, or a port or device investigation that finds Down or Degraded,
   sets the final status. Otherwise the fixed check's status stands.
4. **OpUp is PE-side readiness.** No example below tested customer traffic; the agent cannot.

## l2bridge: Down, explained by a port investigation

**Intent (NSO):** one device, `star-data-sw`, with two attachments in one bridge domain:
`HundredGigE0/0/0/5` VLAN 2002 and the breakout lane `HundredGigE0/0/0/64/0` untagged (VLAN 0).

**Evidence:** on `star-data-sw`: `interfaces brief`, `l2vpn bridge-domain detail`, and
`l2vpn forwarding bridge-domain bg-ms-4357c25d…:bd-ms-4357c25d… detail location 0/RP0/CPU0`.

**Checks:**

| Check | Attachment | Verdict | Meaning |
| --- | --- | --- | --- |
| attachment | Hu0/0/0/5.2002 | pass | admin and line state up in `interfaces brief` |
| bridge_state | (the bridge) | pass | the bridge domain's state is up |
| membership | Hu0/0/0/5.2002 | pass | the attachment is a member of that bridge, state up |
| encapsulation | Hu0/0/0/5.2002 | pass | the member's VLAN is the intended 2002 |
| local_forwarding | Hu0/0/0/5.2002 | pass | programmed in the forwarding table, oper up |
| local_flooding | (the bridge) | pass | broadcast and unknown-unicast flooding enabled |
| attachment | Hu0/0/0/64/0.0 | **fault** | the lane is down |
| membership | Hu0/0/0/64/0.0 | **fault** | a member, but its state is down |
| encapsulation | Hu0/0/0/64/0.0 | pass | untagged, as intended |
| local_forwarding | Hu0/0/0/64/0.0 | unknown | not found as one programmed member |

**Status:** two faults, so the fixed check says **Down**. Three other services were down on the
same port (lanes 64/1 to 64/3), so the port was investigated once instead of each service. It
read the optics: every lane of `HundredGigE0/0/0/64` received −40 dBm with loss of
signal while sending normally. The port investigation found the port down, so the final status
is **Down — port investigated once**.

**Follow-up in the report:** inspect, clean and reseat the optic and the breakout cabling on
site, and have the far end's transmit power read. The fault points away from this router (no
light arriving), but the far end was not read.

## l2sts: OpUp from the fixed checks alone

**Intent (NSO):** two sites, `dall-data-sw` `HundredGigE0/0/0/2` and `tacc-data-sw`
`HundredGigE0/0/0/4`, both untagged, joined by EVPN.

**Evidence:** on each device the same three bridge reads as l2bridge, then
`evpn evi vpn-id 9101 detail` and `evpn evi vpn-id 9101 inclusive-multicast detail`, and finally
`cef 10.130.128.1/32` on dall and `cef 10.133.128.1/32` on tacc (each other's loopback).

**Checks:** the bridge checks above on both devices (all pass), plus `evpn_binding` (the bridge's
EVPN state is up), then for EVPN:

| Check | Device | Recorded |
| --- | --- | --- |
| effective_evpn | dall-data-sw | import and export RT 398900:9101; PE 10.133.128.1; label 24129 |
| effective_evpn | tacc-data-sw | import and export RT 398900:9101; PE 10.130.128.1; label 24121 |
| rt_compatibility | each direction | the receiver imports what the sender exports |
| replication | each direction | the sender floods to the receiver's loopback with the receiver's own label |
| transport | each direction | on the sender, the receiver's loopback resolves to a labelled path in CEF |

All 22 checks pass, in both directions, so the service is **OpUp — basic operational pass**.
Nothing further is investigated.

**What it does not say:** the RTs are the ones in effect on the routers, not the configured
ones; an empty MAC table would not have failed it, and no customer traffic was sent.

## l3rt: Unknown from the fixed check, OpUp after an LLM investigation

**Intent (NSO):** attachment `ncsa-data-sw` `HundredGigE0/0/0/15` VLAN 1814, IPv6 only, gateway
`2001:db8:9:19::1/64`, external access through border routers `star-data-sw` and
`wash-data-sw`, permitted host `2001:db8:9:19::2`.

**Evidence:** on ncsa, the bridge reads, `interfaces BVI4059`,
`running-config interface BVI4059`, then `route ipv6` and `cef ipv6` for the service /64, the
permitted host and the default route `::/0`. On each border router, `route ipv6` and
`cef ipv6` for the same destinations.

**Checks:**

| Check | Device | Verdict | Recorded |
| --- | --- | --- | --- |
| bridge checks | ncsa-data-sw | pass | Hu0/0/0/15.1814 up, member, VLAN 1814, programmed |
| gateway | ncsa-data-sw | pass | BVI4059 up |
| gateway_address | ncsa-data-sw | pass | `2001:db8:9:19::1/64` configured on BVI4059 |
| rib, fib, gateway_route_binding | ncsa-data-sw | pass | the /64, the host and `::/0` installed; the /64 bound to BVI4059 |
| rib (the /64) | star-data-sw, wash-data-sw | **unknown** | no exact /64 route |
| rib, fib (host, `::/0`) | star-data-sw, wash-data-sw | pass | the host found in the routing table; the covering /48 and `::/0` in CEF |

**Status:** no fault, but two unknowns, so the fixed check says **Unknown**. The service was
picked for an LLM investigation (12 tool calls). It found `% Network not in table` for the exact
/64 on both border routers, and the host reached through the covering `2001:db8:9::/48` IS-IS
route with resolved forwarding entries and reachable next hops. Its completed conclusion was up,
so the final status is **OpUp — LLM concluded OpUp**.

**Follow-up in the report:** nothing to fix. A delivery check that an operator can run from the
permitted host (`ping 2001:db8:9:19::1`), because customer traffic was not tested. Unknown here
was "the fixed check could not tell", not an outage.

## l2ptp: OpUp, with the label followed each way

**Intent (NSO):** a point-to-point circuit between `salt-data-sw` `HundredGigE0/0/0/6` VLAN 100
and `atla-data-sw` `HundredGigE0/0/0/10` VLAN 100, over EVPN VPWS.

**Evidence:** `l2vpn xconnect` on each device (one read per device, shared by all its l2ptp
services), then `l2vpn xconnect group evpn_vpws xc-name evpn_vpws_9000 detail` on both.

**Checks:**

| Check | Device | Recorded |
| --- | --- | --- |
| xconnect | salt-data-sw | `evpn_vpws_9000` UP; attachment Hu0/0/0/6.100 UP; far side EVPN 9081 to 10.138.128.1 |
| xconnect | atla-data-sw | `evpn_vpws_9000` UP; attachment Hu0/0/0/10.100 UP; far side EVPN 9081 to 10.134.0.1 |
| label_path | salt → atla | the sender uses the receiver's label 24083 |
| label_path | atla → salt | the sender uses the receiver's label 28098 |

All pass, so the service is **OpUp — basic operational pass**. Two ends showing different
label numbers would not be a fault; only a label that cannot be followed to the receiver's own
label is, and that gives Unknown, never Down.

## l3rt: Unknown because the device's sync state could not be read

**Intent (NSO):** a service on `cien-data-sw` only.

**Evidence:** none from the device. NSO could not connect to `cien-data-sw` this scan, so its sync
state reads `error`.

**Status:** the sync step fails, the fixed checks do not run ("Sync prerequisite not
satisfied"), and the service is **Unknown — Sync unknown**. This is missing evidence, not proof
of an outage: the report lists the device under its collection gaps, and the service stays Unknown
until a scan can read the device.
