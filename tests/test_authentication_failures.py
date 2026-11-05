"""Devices NSO could not authenticate to are named in the Result, the follow-up and their own blocks."""
from diagnostic_mas.case import Budget, CaseFile, add_evidence, open_issue
from diagnostic_mas.case_delta import compute_case_delta
from diagnostic_mas.newness import CARRIED, NEW, Newness, Seen, newness_for
from diagnostic_mas.nso_authentication import authentication_failures
from diagnostic_mas.operator_report import format_devices_operator, format_followup_operator, format_result_section
from diagnostic_mas.state_paths import case_to_dict

KEY = "SSH host key mismatch"


def _refused(device):
    return f"RESTCONF 500: Failed to authenticate towards device {device}: {KEY}"


def _case():
    case = CaseFile(budget=Budget(0, 0))
    case.device_names = ["atla-data-sw", "brist-data-sw", "cern-data-sw", "kans-data-sw", "mich-data-sw",
                         "psc-data-sw"]
    open_issue(case, code="collection_error", layer="underlay", evidence_ids=[],
               message=f"kans-data-sw: check_isis_adjacencies failed: {_refused('kans-data-sw')}")
    open_issue(case, code="collection_error", layer="routing", evidence_ids=[],
               message=f"psc-data-sw: BGP summary collection failed: {_refused('psc-data-sw')}")
    open_issue(case, code="collection_error", layer="underlay", evidence_ids=[],
               message="brist-data-sw: check_isis_adjacencies failed: RESTCONF 500: Device brist-data-sw is "
                       "southbound locked")
    add_evidence(case, {"kind": "spine", "role": "service", "layer": "services", "payload": {"extra": {
        "system_health": {"mich-data-sw": {"memory": {"used_pct": 25.2}, "error": _refused("mich-data-sw")},
                          "atla-data-sw": {"cpu": {"one_min": 1}}}}}})
    add_evidence(case, {"kind": "spine", "role": "isis", "layer": "underlay", "payload": {"extra": {
        "failed_then_read": [{"code": "collection_error", "layer": "underlay",
                              "message": f"cern-data-sw: check_isis_adjacencies failed: {_refused('cern-data-sw')}"}]}}})
    return case


DEVICES = ["cern-data-sw", "kans-data-sw", "mich-data-sw", "psc-data-sw"]


def _line(lines, label):
    return next(line for line in lines if line.startswith(f"- **{label}:**"))


def _block(case, device):
    return "\n".join(format_devices_operator(case)).split(f"### {device}\n", 1)[1].split("\n### ", 1)[0]


def test_every_place_the_scan_recorded_nso_failing_to_authenticate_is_found():
    assert authentication_failures(_case()) == {device: [KEY] for device in DEVICES}
    assert authentication_failures(case_to_dict(_case())) == {device: [KEY] for device in DEVICES}


def test_the_result_names_the_devices_nso_could_not_authenticate_to():
    line = _line(format_result_section(_case(), None), "NSO connection")

    assert line == ("- **NSO connection:** NSO failed to authenticate to 4 devices at least once this scan "
                    f"({KEY}): `cern-data-sw`, `kans-data-sw`, `mich-data-sw` and `psc-data-sw`.")


def test_the_result_says_which_of_them_are_new():
    newness = Newness(authentication={"cern-data-sw": Seen(CARRIED), "kans-data-sw": Seen(CARRIED),
                                      "mich-data-sw": Seen(CARRIED), "psc-data-sw": Seen(NEW)})

    line = _line(format_result_section(_case(), newness), "NSO connection")

    assert line.startswith("- **NSO connection:** NSO failed to authenticate to 4 devices (1 new: `psc-data-sw`) "
                           f"at least once this scan ({KEY}):")


def test_the_follow_up_asks_a_person_to_verify_the_host_keys_out_of_band():
    items = "\n".join(format_followup_operator(_case()))

    assert ("Verify the SSH host keys of `cern-data-sw`, `kans-data-sw`, `mich-data-sw` and `psc-data-sw` "
            "out-of-band: NSO rejected the key each presented at least once this scan") in items
    assert "`devices device <name> ssh fetch-host-keys`" in items and "This scan changed nothing." in items


def test_the_follow_up_puts_the_newly_refused_devices_first():
    newness = Newness(authentication={"cern-data-sw": Seen(CARRIED), "kans-data-sw": Seen(CARRIED),
                                      "mich-data-sw": Seen(CARRIED), "psc-data-sw": Seen(NEW)})

    items = [item for item in format_followup_operator(_case(), newness) if "Verify the SSH host keys" in item]

    assert items[0].split(". ", 1)[1].startswith("**New this scan:** Verify the SSH host keys of `psc-data-sw` ")
    assert items[1].split(". ", 1)[1].startswith("**Carried over:** Verify the SSH host keys of `cern-data-sw`, ")


def test_a_device_block_says_a_read_nso_could_not_authenticate_for_was_not_read():
    case = _case()

    assert f"IS-IS not read ({KEY})" in _block(case, "kans-data-sw")
    assert f"**Routing:** BGP not read ({KEY})" in _block(case, "psc-data-sw")
    assert f"**Health:** System health not read in full ({KEY}). " in _block(case, "mich-data-sw")
    assert "not read (" not in _block(case, "atla-data-sw") and "IS-IS N/A" in _block(case, "brist-data-sw")


def test_new_means_the_previous_scan_did_not_record_it():
    case = _case()
    before = CaseFile(budget=Budget(0, 0))
    open_issue(before, code="collection_error", layer="underlay", evidence_ids=[],
               message=f"kans-data-sw: check_isis_adjacencies failed: {_refused('kans-data-sw')}")

    delta = compute_case_delta(case_to_dict(before), case_to_dict(case))
    newness = newness_for(case, delta)

    assert delta["authentication_failed_before"] == ["kans-data-sw"]
    assert newness.authentication == {"cern-data-sw": Seen(NEW), "kans-data-sw": Seen(CARRIED),
                                      "mich-data-sw": Seen(NEW), "psc-data-sw": Seen(NEW)}


def test_a_tag_says_all_new_rather_than_repeating_the_list():
    newness = Newness(authentication={device: Seen(NEW) for device in DEVICES})

    line = _line(format_result_section(_case(), newness), "NSO connection")

    assert line.startswith("- **NSO connection:** NSO failed to authenticate to 4 devices (all new) at least once")


def test_the_health_line_reads_as_sentences_after_the_failed_read():
    case = _case()
    case.evidence[0]["payload"]["extra"]["hardware_health"] = {"mich-data-sw": {"control_plane": [{"dropped": 5}]}}

    health = next(line for line in _block(case, "mich-data-sw").splitlines() if line.startswith("**Health:**"))

    rest = health.split(f"({KEY}). ", 1)[1]
    assert rest[:1] == rest[:1].upper()
