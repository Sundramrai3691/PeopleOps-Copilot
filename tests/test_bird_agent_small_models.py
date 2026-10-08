import io
import json
import pytest
from scripts.evaluation.evaluate_bird_agent import LoggedModel


@pytest.mark.parametrize('disable', [True, False])
def test_explicit_non_thinking_payload(monkeypatch, disable):
    captured = []
    def fake_open(request, timeout):
        captured.append(json.loads(request.data))
        return io.BytesIO(json.dumps({'response': 'SELECT 1', 'message': {'content': 'SELECT 1'},
            'done_reason': 'stop', 'prompt_eval_count': 10, 'eval_count': 3}).encode())
    monkeypatch.setattr('urllib.request.urlopen', fake_open)
    model = LoggedModel('qwen3:4b', max_tokens=2048, disable_thinking=disable)
    assert model.complete('SQL please') == 'SELECT 1'
    if disable:
        assert captured[0]['think'] is False
    else:
        assert 'think' not in captured[0]
    assert captured[0]['options']['num_predict'] == 2048
    assert len(model.calls) == 1


def test_qwen_non_thinking_bypasses_forced_think_template(monkeypatch):
    captured = []
    def fake_open(request, timeout):
        captured.append((request.full_url,json.loads(request.data)))
        return io.BytesIO(json.dumps({'response': 'SELECT 1', 'done_reason': 'stop'}).encode())
    monkeypatch.setattr('urllib.request.urlopen',fake_open)
    model=LoggedModel('qwen3:4b',disable_thinking=True)
    assert model.complete('Return SELECT 1')=='SELECT 1'
    url,body=captured[0]
    assert url.endswith('/api/generate') and body['raw'] is True
    assert body['prompt'].endswith('<|im_start|>assistant\n<think>\n\n</think>\n\n')


def test_generic_planner_decodes_structured_sql(tmp_path):
    import sqlite3
    from scripts.evaluation.evaluate_bird_agent import BirdPlanner
    from sql_agent.mutations import MutationPolicy, MutationService
    from sql_agent.retrieval import KnowledgeRetriever
    path=tmp_path/'data.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE things(value INT)')
    service=MutationService(tmp_path/'control.sqlite', {'demo':MutationPolicy(path,('things',))})
    class Model:
        sql_only=True
        def complete_with_metadata(self,prompt):
            assert 'JSON object' in prompt
            return '{"sql":"SELECT value FROM things"}', {'done_reason':'stop'}
    planner=BirdPlanner(service,Model(),Model(),KnowledgeRetriever(),profile='generic',schema_mode='full',use_rag=False)
    assert planner('demo','List values')=='SELECT value FROM things'
