from agent.system_health import parse_memory_summary, parse_processes_cpu

CPU_SAMPLE = """
---- node0_RP0_CPU0 ----

CPU utilization for one minute: 2%; five minutes: 3%; fifteen minutes: 4%

PID    1Min    5Min    15Min Process
1        0%      0%       0% init
"""

MEM_SAMPLE = """
node:      node0_RP0_CPU0
------------------------------------------------------------------

 Physical Memory: 18432M total (12151M available)
 Application Memory : 18432M (12151M available)
"""


def test_parse_processes_cpu_header_only():
    parsed = parse_processes_cpu(CPU_SAMPLE)
    assert parsed == {
        "one_min": 2,
        "five_min": 3,
        "fifteen_min": 4,
        "node": "node0_RP0_CPU0",
    }


def test_parse_memory_summary():
    parsed = parse_memory_summary(MEM_SAMPLE)
    assert parsed is not None
    assert parsed["total_mb"] == 18432
    assert parsed["available_mb"] == 12151
    assert parsed["used_pct"] == 34.1


def test_parse_cpu_empty():
    assert parse_processes_cpu("") is None
    assert parse_memory_summary("nope") is None
