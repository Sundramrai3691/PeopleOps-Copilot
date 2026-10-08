import io
import json

import pytest

from sql_agent.model_recovery import complete_json
from sql_agent.planner import OllamaPlannerModel, OpenAICompatiblePlannerModel
from sql_agent.telemetry import summarize_calls


def test_token_cost_includes_retries_and_keeps_unknown_counts_unknown(monkeypatch):
    monkeypatch.setattr('sql_agent.model_recovery.time.sleep', lambda _: None)
    class Model:
        responses = iter(['invalid', '{"sql":"SELECT 1"}'])
        def complete_with_metadata(self, prompt):
            return next(self.responses), {'prompt_tokens': 20, 'completion_tokens': 5}
    events = []
    complete_json(Model(), 'question', events)
    usage = summarize_calls({'planner': events}, 1)
    assert usage['token_cost'] == {'input_tokens': 40, 'output_tokens': 10,
                                   'total_tokens': 50, 'observed_total_tokens': 50,
                                   'complete': True, 'unit': 'tokens'}
    events.append({'event':'failure','attempt':1, 'prompt_tokens':None,'completion_tokens':None})
    cost = summarize_calls({'planner': events}, 1)['token_cost']
    assert cost['total_tokens'] is None
    assert cost['input_tokens'] is None and cost['output_tokens'] is None
    assert cost['observed_total_tokens'] == 50
    assert not cost['complete']


def test_retries_count_failed_response_tokens_without_leaking_content(monkeypatch):
    monkeypatch.setattr('sql_agent.model_recovery.time.sleep', lambda _: None)
    class Model:
        model = 'test-model'
        responses = iter(['private malformed response', '{"action":"STOP"}'])
        def complete_with_metadata(self, prompt):
            return next(self.responses), {'prompt_tokens': 20, 'completion_tokens': 5,
                                          'secret': 'credential-do-not-log'}
    events = []
    complete_json(Model(), 'private prompt', events)
    summary = summarize_calls({'primary': events, 'checker': []}, 1000)
    assert summary['calls'] == 2 and summary['retries'] == 1
    assert summary['failed_calls'] == 1
    assert summary['prompt_tokens_observed'] == 40
    assert summary['completion_tokens_observed'] == 10
    assert summary['usage_complete']
    assert summary['stages']['primary']['retry_wait_ms'] == 250
    assert summary['monetary_cost'] is None
    encoded = json.dumps(events)
    assert 'private' not in encoded and 'credential' not in encoded
    assert all(e['elapsed_ms'] >= 0 for e in events if e['event'] != 'start')


def test_missing_usage_is_unknown_not_free():
    class Model:
        def complete(self, prompt):
            return '{"action":"STOP"}'
    events = []
    complete_json(Model(), 'query', events)
    summary = summarize_calls({'primary': events}, 1)
    assert summary['calls_without_complete_usage'] == 1
    assert not summary['usage_complete']
    assert summary['monetary_cost'] is None


def test_timeout_retries_are_counted_even_without_responses(monkeypatch):
    monkeypatch.setattr('sql_agent.model_recovery.time.sleep', lambda _: None)
    class Model:
        def complete(self, prompt):
            raise TimeoutError('private connection details')
    events = []
    with pytest.raises(TimeoutError):
        complete_json(Model(), 'query', events)
    result = summarize_calls({'checker': events}, 2)
    assert (result['calls'], result['failed_calls'], result['retries']) == (3, 3, 2)
    assert result['calls_without_complete_usage'] == 3
    assert 'private' not in json.dumps(events)


@pytest.mark.parametrize('adapter', [OllamaPlannerModel, OpenAICompatiblePlannerModel])
@pytest.mark.parametrize('with_usage', [True, False])
def test_provider_usage_preserved_and_missing_counts_not_invented(adapter, with_usage, monkeypatch):
    body = {'message': {'content': '{"action":"STOP"}'},
            'choices': [{'message': {'content': '{"action":"STOP"}'}}]}
    if with_usage:
        body.update(prompt_eval_count=21, eval_count=7,
                    usage={'prompt_tokens': 21, 'completion_tokens': 7})
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: io.BytesIO(json.dumps(body).encode()))
    events = []
    complete_json(adapter('http://unused', 'test'), 'query', events)
    summary = summarize_calls({'primary': events}, 1)
    assert summary['prompt_tokens_observed'] == (21 if with_usage else 0)
    assert summary['completion_tokens_observed'] == (7 if with_usage else 0)
    assert summary['usage_complete'] is with_usage
