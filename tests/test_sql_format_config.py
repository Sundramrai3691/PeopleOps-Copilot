import pytest

from sql_agent.sql_config import sql_planner_from_env
from sql_agent.native_sql import NativeSQLPlanner
from sql_agent.data_agent import SQLAgentPlanner


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('SQL_AGENT_PLANNER_BASE_URL','http://127.0.0.1:11434')
    monkeypatch.setenv('SQL_AGENT_PLANNER_MODEL','qwen2.5-coder:14b')
    monkeypatch.setenv('SQL_AGENT_PLANNER_PROVIDER','ollama')
    for key in ('SQL_AGENT_PLANNER_MAX_TOKENS','SQL_AGENT_PLANNER_THINKING',
                'SQL_AGENT_PLANNER_TIMEOUT_SECONDS','SQL_AGENT_SQL_GENERATION_FORMAT'):
        monkeypatch.delenv(key,raising=False)


def test_sql_format_uses_native_protocol_and_does_not_auto_add_json_checker(configured,monkeypatch):
    monkeypatch.setenv('SQL_AGENT_SQL_GENERATION_FORMAT','sql')
    planner=sql_planner_from_env()
    assert isinstance(planner,NativeSQLPlanner)
    assert planner.model.json_mode is False and planner.model.max_tokens==2048
    from sql_agent.pipeline import JobPipeline
    from sql_agent.sql_config import SQLTaskRegistry
    pipeline=JobPipeline(SQLTaskRegistry(),planner)
    assert pipeline.reviewer is None


def test_default_json_configuration_remains_compatible(configured,monkeypatch):
    monkeypatch.delenv('SQL_AGENT_SQL_GENERATION_FORMAT',raising=False)
    planner=sql_planner_from_env()
    assert isinstance(planner,SQLAgentPlanner)
    assert planner.model.json_mode and planner.model.max_tokens==256


def test_unknown_generation_format_fails_early(configured,monkeypatch):
    monkeypatch.setenv('SQL_AGENT_SQL_GENERATION_FORMAT','unknown')
    with pytest.raises(ValueError):sql_planner_from_env()


def test_thinking_profile_has_explicit_bounded_budget(configured,monkeypatch):
    monkeypatch.setenv('SQL_AGENT_SQL_GENERATION_FORMAT','sql')
    monkeypatch.setenv('SQL_AGENT_PLANNER_THINKING','true')
    model=sql_planner_from_env().model
    assert model.thinking and model.max_tokens==8192 and model.timeout==120


def test_thinking_rejects_json_profile(configured,monkeypatch):
    monkeypatch.setenv('SQL_AGENT_SQL_GENERATION_FORMAT','json')
    monkeypatch.setenv('SQL_AGENT_PLANNER_THINKING','true')
    with pytest.raises(ValueError,match='thinking requires'):sql_planner_from_env()


def test_thinking_wire_payload_and_usage(monkeypatch):
    import io
    import json
    from sql_agent.planner import OllamaPlannerModel
    captured=[]
    def open_request(request,timeout):
        captured.append((json.loads(request.data),timeout))
        return io.BytesIO(json.dumps({'message':{'content':'SELECT 1','thinking':'private'},
            'prompt_eval_count':12,'eval_count':345,'done_reason':'stop'}).encode())
    monkeypatch.setattr('urllib.request.urlopen',open_request)
    model=OllamaPlannerModel('http://local','qwen3:14b',timeout=120,max_tokens=8192,json_mode=False,thinking=True)
    text,usage=model.complete_with_metadata('question')
    assert text=='SELECT 1' and 'private' not in str(usage)
    assert usage['completion_tokens']==345
    payload,timeout=captured[0]
    assert payload['think'] is True and 'format' not in payload and timeout==120
    assert payload['options']=={'temperature':.6,'num_predict':8192,'top_p':.95,'top_k':20,'min_p':0,'num_ctx':16384,'seed':917}
