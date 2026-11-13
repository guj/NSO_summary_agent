"""The Result list and the follow-up items say whether each problem is new this scan or carried over."""
from diagnostic_mas.case import open_issue
from diagnostic_mas.case_delta import issue_key
from diagnostic_mas.html_report import notification_digest
from diagnostic_mas.item_history import routing_key
from diagnostic_mas.newness import CARRIED, NEW, UNREAD, Newness, Seen, newness_for
from diagnostic_mas.operator_report import format_followup_operator, format_result_section
from diagnostic_mas.report import render_report
from test_diagnostic_mas_report import _faulted_case
from test_routing_session_down_report import _read_at_one_end

STAR, CAPE, SALT, CIEN, DALL = "star-data-sw", "cape-data-sw", "salt-data-sw", "cien-data-sw", "dall-data-sw"
STAR_CAPE, DALL_CIEN, SALT_CIEN = (f"bgp:10.0.0.1:10.0.0.2:{a}:{b}" for a, b in
                                   ((STAR, CAPE), (DALL, CIEN), (SALT, CIEN)))
SALT_SEAT = "isis:salt-data-sw:?:seat-data-sw:HundredGigE0/0/0/23.3000"
OLD, LATER = "20261027T000000Z", "20261030T000000Z"


def _case():
    case = _read_at_one_end((STAR, CAPE, "idle", "unknown"), (DALL, CIEN, "idle", "unknown"),
                            (SALT, CIEN, "idle", "unknown"))
    open_issue(case, code="unidirectional_adjacency", severity="high", layer="underlay", evidence_ids=[],
               edge_id=SALT_SEAT, message="IS-IS adjacency down")
    open_issue(case, code="unknown_neighbor_address", severity="low", layer="routing", evidence_ids=[],
               message="wash-data-sw: BGP neighbor 10.128.128.1 matches no NSO device")
    return case


def _all_carried(**changes):
    routing = {edge: Seen(CARRIED, OLD, False) for edge in (STAR_CAPE, DALL_CIEN, SALT_CIEN, SALT_SEAT)}
    routing.update(changes)
    return Newness(routing=routing)


def _line(lines, label):
    return next(line for line in lines if line.startswith(f"- **{label}:**"))


def test_without_a_previous_scan_the_result_lists_each_kind_on_its_own_line_untagged():
    lines = format_result_section(_case(), None)

    assert lines[0] == "**Result:**"
    assert _line(lines, "BGP") == (
        "- **BGP:** 3 BGP sessions not established, as read at one end only: to `cien-data-sw` from "
        "`dall-data-sw` and `salt-data-sw`; to `cape-data-sw` from `star-data-sw`.")
    assert _line(lines, "IS-IS") == "- **IS-IS:** 1 IS-IS adjacency down: `salt-data-sw` ↔ `seat-data-sw`."
    assert _line(lines, "Unmatched neighbors") == "- **Unmatched neighbors:** 1 BGP neighbor matches no NSO device."
    order = [lines.index(_line(lines, label)) for label in ("BGP", "IS-IS", "Unmatched neighbors")]
    assert order == sorted(order)


def test_the_result_tags_items_the_previous_scan_also_saw():
    lines = format_result_section(_case(), _all_carried())

    assert _line(lines, "BGP").startswith(
        "- **BGP:** 3 BGP sessions not established (none new), as read at one end only: to `cien-data-sw`")
    assert _line(lines, "IS-IS") == "- **IS-IS:** 1 IS-IS adjacency down (not new): `salt-data-sw` ↔ `seat-data-sw`."
    assert _line(lines, "Unmatched neighbors") == (
        "- **Unmatched neighbors:** 1 BGP neighbor matches no NSO device (not new).")


def test_the_result_tag_names_new_items_and_counts_those_the_last_scan_did_not_read():
    lines = format_result_section(_case(), _all_carried(
        **{STAR_CAPE: Seen(NEW), DALL_CIEN: Seen(UNREAD), SALT_SEAT: Seen(UNREAD)}))

    assert _line(lines, "BGP").startswith(
        "- **BGP:** 3 BGP sessions not established (1 new: `star-data-sw` ↔ `cape-data-sw`; "
        "1 not read last scan), as read at one end only:")
    assert _line(lines, "IS-IS").startswith("- **IS-IS:** 1 IS-IS adjacency down (not read last scan):")


def test_the_services_line_says_which_services_newly_failed():
    case = _faulted_case({name: ("l2bridge", ["mich-data-sw"]) for name in ("b1", "b2", "b3")})

    carried = format_result_section(case, Newness(services={"b1": Seen(CARRIED, OLD, True)}))
    new = format_result_section(case, Newness(services={"b3": Seen(NEW)}))

    assert _line(carried, "Services") == "- **Services:** 0 OpUp, 3 Down, 0 Degraded, 0 Unknown (none newly failed)."
    assert _line(new, "Services") == "- **Services:** 0 OpUp, 3 Down, 0 Degraded, 0 Unknown (1 newly failed: `b3`)."


def test_follow_up_items_say_new_or_the_day_they_were_first_seen():
    items = format_followup_operator(_case(), _all_carried())

    assert items[0].startswith("1. **Open since 27 Oct or earlier:** Investigate 1 IS-IS adjacency down")
    assert items[1].startswith("2. **Open since 27 Oct or earlier:** Investigate 3 BGP sessions not established")
    assert items[2] == "3. **Carried over:** Identify the device behind 1 BGP neighbor that matches no NSO device."


def test_a_group_first_seen_on_different_days_gives_the_earliest_and_the_latest():
    items = format_followup_operator(_case(), _all_carried(**{SALT_CIEN: Seen(CARRIED, LATER, True)}))

    assert "**Open since 27 Oct or earlier; some since 30 Oct:** Investigate 3 BGP sessions" in "\n".join(items)


def test_a_new_item_goes_above_the_carried_ones_of_its_kind_only():
    items = format_followup_operator(_case(), _all_carried(**{STAR_CAPE: Seen(NEW)}))

    assert items == [
        "1. **New this scan:** Investigate 1 BGP session not established, as read at one end only: "
        "to `cape-data-sw` from `star-data-sw`.",
        items[1],
        "3. **Open since 27 Oct or earlier:** Investigate 2 BGP sessions not established, as read at one end "
        "only: to `cien-data-sw` from `dall-data-sw` and `salt-data-sw`.",
        "4. **Carried over:** Identify the device behind 1 BGP neighbor that matches no NSO device.",
    ]
    assert items[1].startswith("2. **Open since 27 Oct or earlier:** Investigate 1 IS-IS adjacency down")


def test_a_grouped_service_fault_is_split_into_its_new_and_carried_services():
    case = _faulted_case({name: ("l2bridge", ["mich-data-sw"]) for name in ("b1", "b2", "b3")})
    newness = Newness(services={"b1": Seen(CARRIED, OLD, False), "b2": Seen(CARRIED, OLD, False),
                                "b3": Seen(NEW)})

    items = [item for item in format_followup_operator(case, newness) if "Prioritize dataplane" in item]

    assert items[0].startswith("1. **New this scan:** Prioritize dataplane down on `b3`:")
    assert items[1].startswith("2. **Open since 27 Oct or earlier:** Prioritize dataplane down on 2 l2bridge "
                               "services on `mich-data-sw`:")
    assert items[1].endswith("Services: `b1`, `b2`.")


def test_the_follow_up_for_services_behind_devices_nso_cannot_reach_has_their_label(monkeypatch):
    from test_sync_recheck import _sync_failed_case

    case = _sync_failed_case(monkeypatch)
    unreachable = [i for i in case.issues if i.get("code") == "device_live_unreachable"]

    carried = format_followup_operator(case, Newness())
    new = format_followup_operator(case, Newness(new_issue_keys=frozenset(issue_key(i) for i in unreachable)))

    assert len(unreachable) == 2
    assert any("**Carried over:** Check NSO's management connection to `cien-data-sw` and `star-data-sw`" in item
               for item in carried)
    assert new[0].startswith("1. **New this scan:** Check NSO's management connection to `cien-data-sw` and")


def test_newness_comes_from_the_item_history_and_the_comparison_with_the_previous_scan():
    case = _case()
    unmatched = case.issues[-1]
    case.item_history = {
        routing_key(STAR_CAPE): {"recent": "SS", "seen": 2, "read": 2, "scans": 2,
                                 "first_seen_run_id": OLD, "first_seen_exact": False},
        routing_key(DALL_CIEN): {"recent": "SCS", "seen": 2, "read": 3, "scans": 3,
                                 "first_seen_run_id": OLD, "first_seen_exact": True},
        routing_key(SALT_CIEN): {"recent": "S-S", "seen": 2, "read": 2, "scans": 3,
                                 "first_seen_run_id": OLD, "first_seen_exact": True},
        routing_key(SALT_SEAT): {"recent": "S", "seen": 1, "read": 1, "scans": 1,
                                 "first_seen_run_id": "20261105T000000Z", "first_seen_exact": True},
    }
    case.last_known_service_faults = [{"service": "b1", "status": "down", "first_observed_run_id": OLD,
                                       "first_observed_exact": True}]
    delta = {"newly_failed_services": [{"service": "b2", "status": "Down"}], "new_problems": [unmatched]}

    newness = newness_for(case, delta)

    def of(edge):
        return newness.of_issue(next(i for i in case.issues if i.get("edge_id") == edge))

    assert of(STAR_CAPE) == Seen(CARRIED, OLD, False)
    assert of(DALL_CIEN) == Seen(NEW)
    assert of(SALT_CIEN) == Seen(UNREAD)
    assert of(SALT_SEAT) == Seen(NEW)
    assert newness.of_issue(unmatched) == Seen(NEW)
    assert newness.of_service("b1") == Seen(CARRIED, OLD, True)
    assert newness.of_service("b2") == Seen(NEW)
    assert newness_for(case, None) is None


def test_an_item_seen_on_and_off_is_carried_over_without_a_day():
    case = _case()
    case.item_history = {routing_key(STAR_CAPE): {"recent": "SCSS", "seen": 3, "read": 4, "scans": 4,
                                                  "first_seen_run_id": OLD, "first_seen_exact": False}}

    newness = newness_for(case, {"new_problems": []})

    assert newness.of_issue(next(i for i in case.issues if i.get("edge_id") == STAR_CAPE)) == Seen(CARRIED)


def test_a_report_compared_with_a_previous_scan_carries_the_tags():
    case = _case()
    case.item_history = {routing_key(SALT_SEAT): {"recent": "SS", "seen": 2, "read": 2, "scans": 2,
                                                  "first_seen_run_id": OLD, "first_seen_exact": False}}

    report = render_report(case, case_delta={"new_problems": []}, include_changes_section=False)

    assert "**Result:**\n" in report
    assert "- **IS-IS:** 1 IS-IS adjacency down (not new): `salt-data-sw` ↔ `seat-data-sw`." in report
    assert "**Open since 27 Oct or earlier:** Investigate 1 IS-IS adjacency down" in report


def test_the_notification_reads_the_service_counts_from_the_result_list():
    report = ("# NSO Diagnostic Report\n**Scope:** 34 devices · 1861 services\n**Duration:** 41 min 1 sec\n"
              "**Result:**\n"
              "- **Services:** 1853 OpUp, 8 Down, 0 Degraded, 0 Unknown (none newly failed).\n"
              "- **NSO connection:** 1 device hit automated check_isis_adjacencies timeout (not new): `scm-data-sw`.\n"
              "\n## Recommended follow-up\n\n1. **Open since 27 Oct or earlier:** Prioritize dataplane down.\n")

    digest = notification_digest(report, "run").splitlines()

    assert digest[3] == "**Result:** 1853 OpUp, 8 Down, 0 Degraded, 0 Unknown."


def test_the_services_line_shortens_the_ids_of_newly_failed_services():
    case = _faulted_case({"net1-9136dfeb-286d-4c3f-bf2f-f86c3b550865": ("l2sts", ["star-data-sw"])})

    line = _line(format_result_section(case, Newness(services={
        "net1-9136dfeb-286d-4c3f-bf2f-f86c3b550865": Seen(NEW)})), "Services")

    assert line.endswith("(1 newly failed: `net1-9136dfeb…`).")
