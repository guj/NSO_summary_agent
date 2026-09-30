from dataclasses import replace
import pytest
from agent.config import _parse_max_retries, load_settings
from agent.summarize import fabric_openai_client
from diagnostic_mas.run_configuration import capture_run_configuration
from diagnostic_mas.case import Budget
from types import SimpleNamespace
from test_diagnostic_mas_publish import _settings

@pytest.mark.parametrize('raw,expected', [(None,0),('0',0),('1',1),('3',3)])
def test_parse(raw,expected):
    assert _parse_max_retries(raw)==expected

@pytest.mark.parametrize('raw', ['', '-1','1.5','no'])
def test_invalid(raw):
    with pytest.raises(ValueError, match='FABRIC_CHAT_MAX_RETRIES'):
        _parse_max_retries(raw)

def test_settings_client_override_and_report(monkeypatch,tmp_path):
    monkeypatch.setenv('NSO_ADDRESS','localhost')
    monkeypatch.setenv('NSO_PASSWORD','test')
    monkeypatch.setenv('FABRIC_AI_API_KEY','test')
    monkeypatch.setenv('FABRIC_CHAT_MAX_RETRIES','2')
    assert load_settings().fabric_max_retries==2
    settings=replace(_settings(state_dir=tmp_path),fabric_max_retries=2)
    client=fabric_openai_client(settings)
    assert client.max_retries==2
    client.close()
    client=fabric_openai_client(settings,max_retries=0)
    assert client.max_retries==0
    client.close()
    result=capture_run_configuration(SimpleNamespace(),settings,Budget(0,0),skip_llm=False,dry_run=True)
    assert result['LLM automatic retries']==2

@pytest.mark.parametrize('raw', ['0','-1','nan','inf','','bad'])
def test_invalid_timeout(raw):
    from agent.config import _parse_chat_timeout
    with pytest.raises(ValueError,match='FABRIC_CHAT_TIMEOUT_SEC'):
        _parse_chat_timeout(raw)


def test_timeout_environment_client_report_and_dump(monkeypatch,tmp_path):
    import json
    from agent.config import _parse_chat_timeout
    from diagnostic_mas.dataplane_verify import _dump_failed_dataplane_llm_request
    assert _parse_chat_timeout(None)==60.0
    monkeypatch.setenv('NSO_ADDRESS','localhost')
    monkeypatch.setenv('NSO_PASSWORD','test')
    monkeypatch.setenv('FABRIC_AI_API_KEY','test')
    monkeypatch.setenv('FABRIC_CHAT_TIMEOUT_SEC','200.0')
    monkeypatch.setenv('FABRIC_CHAT_MAX_RETRIES','0')
    settings=load_settings()
    assert settings.fabric_chat_timeout_sec==200.0
    client=fabric_openai_client(settings)
    assert client.timeout.read==200.0
    assert client.timeout.connect==20.0
    assert client.max_retries==0
    client.close()
    result=capture_run_configuration(SimpleNamespace(),settings,Budget(0,0),skip_llm=False,dry_run=True)
    assert result['Dataplane/drill LLM request timeout (seconds)']==200.0
    monkeypatch.setenv('DATAPLANE_LLM_DUMP',str(tmp_path))
    path=_dump_failed_dataplane_llm_request(service_name='test',round_i=1,model='test',
        messages=[],tools=[],error=TimeoutError(),elapsed_s=200,timeout_sec=200)
    assert json.loads(path.read_text())['timeout_sec']==200.0


@pytest.mark.parametrize("raw", ["", "0", "-1", "nan", "inf", "bad"])
def test_invalid_connect_timeout(raw):
    from agent.config import _parse_connect_timeout
    with pytest.raises(ValueError, match="FABRIC_CHAT_CONNECT_TIMEOUT_SEC"):
        _parse_connect_timeout(raw)


def test_connect_timeout_env_client_and_report(monkeypatch, tmp_path):
    from agent.config import _parse_connect_timeout
    assert _parse_connect_timeout(None) == 20.0
    monkeypatch.setenv("NSO_ADDRESS", "localhost")
    monkeypatch.setenv("NSO_PASSWORD", "test")
    monkeypatch.setenv("FABRIC_AI_API_KEY", "test")
    monkeypatch.setenv("FABRIC_CHAT_CONNECT_TIMEOUT_SEC", "30")
    settings = load_settings()
    client = fabric_openai_client(settings, timeout=120)
    assert client.timeout.connect == 30
    assert client.timeout.read == 120
    client.close()
    result = capture_run_configuration(SimpleNamespace(), settings, Budget(0,0), skip_llm=False,dry_run=True)
    assert result["LLM connection timeout (seconds)"] == 30


@pytest.mark.parametrize("name,label", [("ConnectTimeout", "connection_timeout"), ("ReadTimeout", "response_read_timeout"), ("WriteTimeout", "request_write_timeout"), ("PoolTimeout", "connection_pool_timeout"), ("ConnectError", "connection_error")])
def test_sdk_error_cause_classification(name, label):
    import httpx
    from openai import APITimeoutError
    from agent.summarize import llm_error_category
    outer = APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))
    outer.__cause__ = getattr(httpx, name)("test")
    assert llm_error_category(outer) == label
    outer.__cause__ = None
    assert llm_error_category(outer) == "timeout_unspecified"
