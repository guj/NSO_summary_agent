import json
import pytest
from diagnostic_mas.case import CaseFile, Budget
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


def test_counts_exclusive_typed_identity_and_html():
    c=CaseFile(Budget(0,0))
    one=service("down")
    two=service("unknown"); two["service_type"]="l3rt"
    c.evidence=[dict(kind="spine",role="service",payload={"extra":{"services":{"l2sts/s":one,"l3rt/s":two}}})]
    c.diagnoses=[dict(kind="dataplane",subject={"name":"s","service_type":"l3rt"},source="llm",status="up",complete=True)]
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
            c.diagnoses.append(dict(kind="dataplane", source="llm", status="unknown",
                complete=False, subject={"service_type":"l2sts", "name":f"s{i}"}))
    for status in ("down", "degraded"):
        rec = service(status)
        rec["name"] = status
        records[f"l2sts/{status}"] = rec
        c.diagnoses.append(dict(kind="dataplane", source="llm", status="unknown",
            complete=False, subject={"service_type":"l2sts", "name":status}))
    c.evidence = [dict(kind="spine", role="service", payload={"extra":{"services":records}})]
    assert "Final service status: 0 OpUp, 1 Down, 1 Degraded, 36 Unknown." in format_result_line(c)
    assert final_service_counts(c)["l2sts"]["unknown"] == 36
