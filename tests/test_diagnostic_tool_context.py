"""Generic schema delivery and bounded investigation regressions."""
import json
from types import SimpleNamespace as NS

import pytest

from diagnostic_mas.tool_context import BatchProgress, result_error, tool_catalog
from nso_facts.mcp_client import start_mcp_cache, stop_mcp_cache


@pytest.mark.asyncio
async def test_catalog_preserves_schemas_and_caches_discovery():
    class Client:
        calls = 0
        async def list_tools(self):
            self.calls += 1
            return [NS(name="exec_show", description="Execute show", inputSchema={
                "type": "object", "properties": {"device_name": {"type": "string"}},
                "required": ["device_name"]}),
                NS(name="legacy", description="Legacy", inputSchema={
                    "type": "object", "properties": {"params": {"type": "object"}}}),
                NS(name="write_config", description="Not allowed", inputSchema={})]
    client = Client()
    start_mcp_cache()
    try:
        text = await tool_catalog(client, frozenset({"exec_show", "legacy"}))
        await tool_catalog(client, frozenset({"exec_show"}))
        assert client.calls == 1
        assert '"required": ["device_name"]' in text
        assert 'runner adds the server params wrapper' in text
        assert 'write_config' not in text
    finally:
        stop_mcp_cache()


def test_errors_do_not_confuse_failed_health_with_failed_tool():
    assert result_error(json.dumps({"result": "% Invalid input detected at '^' marker."}))
    assert result_error('{"status":"error","error_message":"timeout"}')
    assert result_error('ERROR: blocked')
    assert not result_error('{"status":"down","errors":123,"interface":{"error":42}}')
    assert not result_error('AC down; remote segment down')


def test_progress_requires_two_consecutive_unproductive_batches():
    progress = BatchProgress()
    assert not progress.observe([('show', 'device A: AC down')])
    assert not progress.observe([('show', 'device A: AC down')])
    assert not progress.observe([('show', 'device B: AC up')])
    assert not progress.observe([('show', 'ERROR: invalid')])
    assert progress.observe([])


@pytest.mark.asyncio
async def test_loop_stops_repeated_errors_and_supplies_schema(monkeypatch):
    from diagnostic_mas import dataplane_verify as dv
    from diagnostic_mas.case import Budget, CaseFile, DrillSession

    class Client:
        async def list_tools(self):
            return [NS(name="exec_show", description="Show", inputSchema={
                "type": "object", "properties": {"device_name": {"type": "string"}}})]

    class LLM:
        def __init__(self):
            self.chat = self.completions = self
            self.calls = 0
        def create(self, **kwargs):
            self.calls += 1
            assert any('input_schema' in str(m['content']) for m in kwargs['messages'])
            if self.calls == 3:
                assert len(kwargs['tools']) == 1
                assert kwargs['tools'][0]['function']['name'] == 'conclude_dataplane'
                raise TimeoutError('test timeout')
            assert self.calls < 3
            call = NS(id=str(self.calls), function=NS(name='mcp_call', arguments=json.dumps({
                'tool_name': 'exec_show', 'params': {'device_name': 'd', 'input_command': 'interfaces'},
                'reason': 'Check AC state'})))
            return NS(choices=[NS(message=NS(content=None, tool_calls=[call]))])

    async def execute(*args, **kwargs):
        return "% Invalid input detected at '^' marker."

    monkeypatch.setattr(dv, 'execute_one_drill_call', execute)
    case = CaseFile(budget=Budget(max_deep_checks=0, max_handoffs=0))
    record = {'name': 'svc', 'service_type': 'test', 'system_status': 'up'}
    llm = LLM()
    start_mcp_cache()
    try:
        await dv.llm_dataplane_verify_one(Client(), NS(fabric_model='test'), case,
            record=record, device_names={'d'}, session=DrillSession(max_tools=40), openai_client=llm)
    finally:
        stop_mcp_cache()
    assert llm.calls == 3
    finding = next(e['payload'] for e in case.evidence if e['kind'] == 'dataplane_incomplete')
    assert finding['dataplane_status'] == 'unknown'
    assert len(finding['partial_tool_results']) == 2
    assert 'timed out' in finding['cause']
    assert 'remains unknown' in finding['cause']
    assert all(r['outcome'] == 'error' for r in finding['partial_tool_results'])
    assert 'in-memory case only' in finding['observed']
    assert 'Retained' not in finding['observed']
