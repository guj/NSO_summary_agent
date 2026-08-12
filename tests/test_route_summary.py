from agent.topology.route_summary import (
    format_route_summary_line,
    format_route_summary_lines,
    parse_route_summary,
)

SAMPLE = """
Tue Jul 14 22:53:16.353 UTC
Route Source                     Routes     Backup     Deleted     Memory(bytes)
connected                        4          1          0           1040
local                            5          0          0           1040
application fib_mgr              0          0          0           0
static                           1          0          0           208
bgp 398900                       446448     0          0           92861184
dagr                             0          0          0           0
isis fabric-net                  166        3          0           35192
vxlan                            0          0          0           0
te-client                        0          0          0           0
Total                            446624     4          0           92898664
"""


def test_parse_route_summary():
    parsed = parse_route_summary(SAMPLE)
    assert parsed["total"] == 446624
    assert parsed["sources"]["bgp 398900"] == 446448
    assert parsed["sources"]["isis fabric-net"] == 166
    assert parsed["sources"]["connected"] == 4
    assert parsed["sources"]["application fib_mgr"] == 0


def test_format_route_summary_lines_type_colon_count():
    lines = format_route_summary_lines(parse_route_summary(SAMPLE))
    assert lines[0] == "  routes: 446624"
    assert lines[1] == "    bgp 398900: 446448"
    assert "    isis fabric-net: 166" in lines
    assert "    connected: 4" in lines
    assert all("application fib_mgr" not in ln for ln in lines)
    assert lines[1].startswith("    bgp 398900:")


def test_format_route_summary_unavailable():
    assert format_route_summary_lines({"error": "boom"}) == [
        "  routes: (unavailable)"
    ]
    assert format_route_summary_line(None) is None
    assert format_route_summary_lines(None) == []
