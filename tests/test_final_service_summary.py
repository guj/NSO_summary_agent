import pytest
from diagnostic_mas.case import CaseFile, Budget, normalize_diagnosis
from diagnostic_mas.service_final_status import assessment, final_service_counts, format_final_services_table, STATUSES
from diagnostic_mas.html_report import render_html_report


def service(status="unknown", **kw):
    return dict(name="s",service_type="l2sts",devices=["a"],device_sync={"a":"in-sync"},
                basic_checks={"status":status,"sync_ready":True}, **kw)


@pytest.mark.parametrize("basic,conclusion,complete,expected", [
    ("up",None,None,"up"), ("down",None,None,"down"),
    ("degraded","unknown",False,"degraded"), ("down","unknown",False,"down"),
    ("unknown","up",True,"up"), ("down","up",True,"up"),
    ("unknown","down",True,"down"), ("unknown","degraded",True,"degraded"),
    ("unknown",None,None,"unknown"), ("unknown","unknown",False,"unknown"),
    ("down","up",False,"down")])
def test_final_precedence(basic,conclusion,complete,expected):
    dig = None if conclusion is None else dict(status=conclusion,complete=complete,source="llm")
    assert assessment(service(basic),dig)[0]==expected


@pytest.mark.parametrize("sync,reason",[("out-of-sync","Sync out"),(None,"Sync unknown")])
def test_sync_stop(sync,reason):
    s=service("down"); s["device_sync"]={"a":sync}
    assert assessment(s,dict(status="up",source="llm",complete=True))==("unknown",reason)


@pytest.mark.parametrize("sync,note",[("out-of-sync","sync out"),("locked","sync unknown"),("error","sync unknown"),(None,"sync unknown")])
@pytest.mark.parametrize("source,status,text",[
    ("llm","down","LLM concluded Down"), ("llm","degraded","LLM concluded Degraded"),
    ("port_investigation","down","Down — port investigated once"),
    ("device_investigation","down","Down — device investigated once")])
def test_a_down_or_degraded_conclusion_stands_when_a_device_is_not_in_sync_and_says_so(sync,note,source,status,text):
    s=service("unknown"); s["device_sync"]={"a":sync}
    label=status.capitalize()
    got=assessment(s,dict(status=status,source=source,complete=True))
    assert got[0]==status
    assert got[1]==text.replace(label,f"{label}, {note}",1)


@pytest.mark.parametrize("sync",["out-of-sync",None])
def test_a_fixed_check_down_stands_when_a_device_is_not_in_sync(sync):
    s=service("down"); s["device_sync"]={"a":sync}
    s["basic_checks"]["needs_investigation"]=False
    assert assessment(s)==("down","Down, "+("sync out" if sync else "sync unknown")+" — decided by the fixed check")
    s["basic_checks"]["needs_investigation"]=True                       # the investigation has not run
    assert assessment(s)[0]=="down"


@pytest.mark.parametrize("sync,reason",[("out-of-sync","Sync out"),(None,"Sync unknown")])
def test_up_and_unfinished_results_are_still_held_at_unknown_when_a_device_is_not_in_sync(sync,reason):
    s=service("up"); s["device_sync"]={"a":sync}
    assert assessment(s)==("unknown",reason)                                    # a pass without a trusted baseline
    assert assessment(s,dict(status="up",source="llm",complete=True))==("unknown",reason)
    s2=service("unknown"); s2["device_sync"]={"a":sync}
    assert assessment(s2,dict(status="unknown",source="llm",complete=False))==("unknown",reason)
    assert assessment(s2,dict(status="down",source="llm",complete=False))==("unknown",reason)   # not concluded: no finding to keep


def test_a_down_finding_on_a_device_that_is_not_in_sync_is_counted_down_and_marked_in_the_breakdown():
    c=CaseFile(Budget(0,0))
    one=service("unknown"); one["device_sync"]={"a":"out-of-sync"}
    c.evidence=[dict(kind="spine",role="service",payload={"extra":{"services":{"l2sts/s":one}}})]
    c.diagnoses=[normalize_diagnosis(dict(kind="dataplane",subject={"name":"s","service_type":"l2sts"},source="llm",status="down",complete=True))]
    row=final_service_counts(c)["l2sts"]
    assert (row["down"],row["unknown"])==(1,0)
    assert row["sources"]["down"]=={"LLM concluded Down, sync out":1}


def test_counts_exclusive_typed_identity_and_html():
    c=CaseFile(Budget(0,0))
    one=service("down")
    two=service("unknown"); two["service_type"]="l3rt"
    c.evidence=[dict(kind="spine",role="service",payload={"extra":{"services":{"l2sts/s":one,"l3rt/s":two}}})]
    c.diagnoses=[normalize_diagnosis(dict(kind="dataplane",subject={"name":"s","service_type":"l3rt"},source="llm",status="up",complete=True))]
    rows=final_service_counts(c)
    assert rows["l2sts"]["down"]==1 and rows["l3rt"]["up"]==1
    for row in rows.values():
        assert sum(row[s] for s in STATUSES)==row["total"]
        for status in STATUSES: assert sum(row["sources"][status].values())==row[status]
    md="# Report\n## Services\n"+"\n".join(format_final_services_table(rows))
    html=render_html_report(md,"test")
    assert "<th>OpUp</th>" in html
    assert html.count('class="status-count"')==2
    assert "LLM concluded OpUp: 1" in html
    assert "Operational Down — not investigated: 1" in html
    assert "service-final-breakdown:" not in html
    assert "<summary>Status definitions</summary>" in html


def test_untrusted_type_cannot_close_metadata_comment():
    key="evil--><script>alert(1)</script>"
    rows={key:dict(total=1,up=0,down=0,degraded=0,unknown=1,sources={"unknown":{"Sync unknown":1}})}
    md="\n".join(format_final_services_table(rows))
    metadata=md.split("<!-- ",1)[1].split(" -->",1)[0]
    assert "<script>" not in metadata
    output=render_html_report("# Report\n## Services\n"+md,"test")
    assert "<script>alert(1)</script>" not in output


def test_legacy_sync_only_never_implies_operational_up():
    rec=service(); rec.pop("basic_checks"); rec["status"]="up"
    assert assessment(rec)[0]=="unknown"


def test_headline_matches_final_counts_with_incomplete_digs():
    from diagnostic_mas.operator_report import format_result_line
    c = CaseFile(Budget(0, 0))
    records = {}
    # 15 undug unknowns plus 21 inconclusive digs reproduce the nso41 omission.
    for i in range(36):
        rec = service()
        rec["name"] = f"s{i}"
        records[f"l2sts/s{i}"] = rec
        if i >= 15:
            c.diagnoses.append(normalize_diagnosis(dict(kind="dataplane", source="llm", status="unknown",
                complete=False, subject={"service_type":"l2sts", "name":f"s{i}"})))
    for status in ("down", "degraded"):
        rec = service(status)
        rec["name"] = status
        records[f"l2sts/{status}"] = rec
        c.diagnoses.append(normalize_diagnosis(dict(kind="dataplane", source="llm", status="unknown",
            complete=False, subject={"service_type":"l2sts", "name":status})))
    c.evidence = [dict(kind="spine", role="service", payload={"extra":{"services":records}})]
    assert "Final service status: 0 OpUp, 1 Down, 1 Degraded, 36 Unknown." in format_result_line(c)
    assert final_service_counts(c)["l2sts"]["unknown"] == 36


def _two_services_behind_a_failed_device():
    """s1 has a completed Down finding; s2 has nothing. Both sit on device a, whose sync check errored."""
    def record(name):
        return dict(name=name, service_type="l2sts", devices=["a"], device_sync={"a": "error"}, system_status="unknown",
                    basic_checks={"status": "not_checked", "sync_ready": False})
    c = CaseFile(Budget(0, 0))
    c.evidence = [dict(kind="spine", role="service", payload={"extra": {
        "services": {"l2sts/s1": record("s1"), "l2sts/s2": record("s2")},
        "sync_rechecks": {"a": {"resolved": False}}}})]
    c.diagnoses = [normalize_diagnosis(dict(kind="dataplane", subject={"name": "s1", "service_type": "l2sts"},
                                            source="llm", status="down", complete=True))]
    return c


def test_the_sync_groupings_list_only_the_services_whose_final_status_is_unknown():
    from diagnostic_mas.operator_report import (
        _incomplete_checks_by_endpoint_reason, _verification_unknown_by_endpoints,
        _verification_unknown_involvement_by_device, _verification_unknown_service_names)
    from diagnostic_mas.sync_failure_report import sync_failure_group

    c = _two_services_behind_a_failed_device()
    row = final_service_counts(c)["l2sts"]
    assert (row["down"], row["unknown"]) == (1, 1)                    # s1 counts Down, s2 stays Unknown

    assert _verification_unknown_by_endpoints(c) == [(("a",), 1)]
    assert dict(_verification_unknown_involvement_by_device(c)) == {"a": 1}
    assert _verification_unknown_service_names(c) == {"s2"}
    assert sum(n for _eps, _reason, n in _incomplete_checks_by_endpoint_reason(c)) == 1
    assert sync_failure_group(c) == (["a"], 1)                        # "1 distinct service remains Unknown"
