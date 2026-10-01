import html
import json
import re
from types import SimpleNamespace
from diagnostic_mas.service_topology import service_topology_data, render_service_topology
from diagnostic_mas.service_final_status import final_service_counts
from diagnostic_mas.html_report import render_html_report


def fixture():
    records = {}
    for kind, name, state in [("l2sts", "same", "up"), ("l2ptp", "same", "down"), ("l3rt", "routing", "up")]:
        records[kind+"/"+name] = dict(service_type=kind, name=name, devices=["losa"], device_sync={"losa":"in-sync"}, basic_checks={"status":state,"sync_ready":True,"checks":[{"check":"attachment","device":"losa","status":"pass","observation":"Hu0.100"}]})
    records["l3rt/routing"]["basic_checks"]["intent"] = {"external-access":{"border-router":[{"device":"border"}]}}
    return dict(evidence=[dict(kind="spine",role="service",payload={"extra":{"services":records}})],diagnoses=[dict(kind="dataplane",subject={"service_type":"l2sts","name":"same"},source="llm",status="unknown",complete=False)],device_names=["losa"])


def test_final_status_matches_summary_and_does_not_cross_types():
    c=fixture(); rows=service_topology_data(c)
    totals=final_service_counts(SimpleNamespace(evidence=c["evidence"],diagnoses=c["diagnoses"]))
    for row in rows:
        assert totals[row["type"]][row["status"]] == 1
    assert next(r for r in rows if r["type"]=="l2ptp")["diagnosis"] is None
    assert next(r for r in rows if r["type"]=="l2sts")["status"]=="up"


def test_sync_failure_is_unknown_and_absent_excluded():
    c=fixture(); records=c["evidence"][0]["payload"]["extra"]["services"]
    records["l2ptp/same"]["device_sync"]={"losa":"error"}
    records["l2sts/same"]["presence_recheck"]={"outcome":"absent"}
    rows=service_topology_data(c)
    assert len(rows)==2
    assert next(r for r in rows if r["type"]=="l2ptp")["status"]=="unknown"


def test_roles_and_exact_attachments():
    row=next(r for r in service_topology_data(fixture()) if r["type"]=="l3rt")
    assert row["devices"]==["border","losa"]
    assert row["borders"]==["border"]
    assert row["attachments"]==[{"device":"losa","label":"Hu0.100","status":"pass"}]


def test_untrusted_strings_cannot_escape_frame_or_script():
    c=fixture(); c["evidence"][0]["payload"]["extra"]["services"]["l2sts/same"]["name"]='</script><img src=x onerror=alert(1)>'
    result=render_service_topology(c)
    doc=html.unescape(re.search(r'srcdoc="(.*?)"></iframe>',result,re.S)[1])
    assert '</script><img' not in doc
    assert doc.count('</script>')==1
    assert 'sandbox="allow-scripts"' in result


def test_html_hook_optional_and_no_data_omitted():
    assert render_service_topology(None)==""
    assert render_service_topology({})==""
    assert 'id="service-topology"' not in render_html_report('# Test','run')
    assert 'id="service-topology"' in render_html_report('# Test','run',case=fixture())
