from dataclasses import replace
import pytest
from agent.config import _parse_temperature, llm_temperature_kwargs, load_settings
from test_diagnostic_mas_publish import _settings


@pytest.mark.parametrize('raw,expected', [(None,0.1),('',None),('  ',None),('0',0.0),('1',1.0),('0.1',0.1),('2',2.0)])
def test_parse(raw,expected):
    assert _parse_temperature(raw)==expected


@pytest.mark.parametrize('raw', ['nan','inf','-1','2.1','abc'])
def test_invalid(raw):
    with pytest.raises(ValueError,match='FABRIC_AI_TEMPERATURE'):
        _parse_temperature(raw)


@pytest.mark.parametrize('value', [None,0.0,1.0])
def test_request_kwargs(tmp_path,value):
    settings=replace(_settings(state_dir=tmp_path),fabric_temperature=value)
    captured={}
    def request(**kwargs): captured.update(kwargs)
    request(model=settings.fabric_model,**llm_temperature_kwargs(settings))
    assert captured==({'model':settings.fabric_model} if value is None else {'model':settings.fabric_model,'temperature':value})


def test_env_loading(monkeypatch):
    monkeypatch.setenv('NSO_ADDRESS','localhost')
    monkeypatch.setenv('NSO_PASSWORD','test')
    monkeypatch.setenv('FABRIC_AI_API_KEY','test')
    monkeypatch.setenv('FABRIC_AI_TEMPERATURE','1')
    assert load_settings().fabric_temperature==1
    monkeypatch.setenv('FABRIC_AI_TEMPERATURE','')
    assert load_settings().fabric_temperature is None


def test_unset_temperature_defaults_to_point_one(monkeypatch, tmp_path):
    monkeypatch.setenv('NSO_ADDRESS', 'localhost')
    monkeypatch.setenv('NSO_PASSWORD', 'test')
    monkeypatch.setenv('FABRIC_AI_API_KEY', 'test')
    monkeypatch.delenv('FABRIC_AI_TEMPERATURE', raising=False)
    assert load_settings().fabric_temperature == 0.1
    assert llm_temperature_kwargs(_settings(state_dir=tmp_path)) == {'temperature': 0.1}
