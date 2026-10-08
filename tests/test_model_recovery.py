import json
import urllib.error
import pytest
from sql_agent.data_agent import SQLAgentPlanner, DataContract, PlanningContext, SQLPlanningEvidence
from sql_agent.semantic_review import IndependentSQLPlanner

CTX=PlanningContext('List customer as item, ordered by id.', DataContract(('item',)),
    SQLPlanningEvidence((('orders','CREATE TABLE orders(id INTEGER, customer TEXT)'),),'hash'),1,2)
GOOD='{"action":"REPAIR","sql":"SELECT customer AS item FROM orders ORDER BY id"}'

class SequenceModel:
    def __init__(self,values): self.values=iter(values); self.calls=[]
    def complete(self,prompt):
        self.calls.append(prompt)
        value=next(self.values)
        if isinstance(value,Exception): raise value
        return value

@pytest.mark.parametrize('planner',[SQLAgentPlanner,IndependentSQLPlanner])
@pytest.mark.parametrize('fault',[TimeoutError('transient'),urllib.error.HTTPError('test',429,'limited',{},None),'{"action":'])
def test_transient_model_failure_recovers_with_same_evidence(planner,fault):
    model=SequenceModel([fault,GOOD])
    assert planner(model)(CTX).action=='REPAIR'
    assert len(model.calls)==2 and model.calls[0]==model.calls[1]

@pytest.mark.parametrize('planner',[SQLAgentPlanner,IndependentSQLPlanner])
def test_explicit_stop_never_retried(planner):
    model=SequenceModel(['{"action":"STOP"}',GOOD])
    assert planner(model)(CTX).action=='STOP'
    assert len(model.calls)==1

@pytest.mark.parametrize('planner',[SQLAgentPlanner,IndependentSQLPlanner])
def test_persistent_failure_stops_after_three_attempts(planner,monkeypatch):
    monkeypatch.setattr('sql_agent.model_recovery.time.sleep',lambda _:None)
    model=SequenceModel([TimeoutError('down')]*4)
    instance=planner(model)
    with pytest.raises(TimeoutError): instance(CTX)
    assert len(model.calls)==3
    assert instance.recovery_events[-1]['retry'] is False

@pytest.mark.parametrize('code',[400,401,403,404])
def test_permanent_http_errors_are_not_retried(code):
    model=SequenceModel([urllib.error.HTTPError('test',code,'error',{},None),GOOD])
    with pytest.raises(urllib.error.HTTPError): SQLAgentPlanner(model)(CTX)
    assert len(model.calls)==1

@pytest.mark.parametrize('header',['60','Wed, 21 Oct 2030 07:28:00 GMT','nan'])
def test_retry_after_outside_budget_stops(header):
    model=SequenceModel([urllib.error.HTTPError('test',429,'error',{'Retry-After':header},None),GOOD])
    with pytest.raises(urllib.error.HTTPError): IndependentSQLPlanner(model)(CTX)
    assert len(model.calls)==1


def test_retry_after_honored(monkeypatch):
    delays=[]
    monkeypatch.setattr('sql_agent.model_recovery.time.sleep',delays.append)
    model=SequenceModel([urllib.error.HTTPError('test',429,'error',{'Retry-After':'1'},None),GOOD])
    assert IndependentSQLPlanner(model)(CTX).action=='REPAIR'
    assert delays==[1.0]


def test_valid_json_with_invalid_schema_not_retried():
    model=SequenceModel(['{"action":"REPAIR","sql":5}',GOOD])
    with pytest.raises(ValueError): IndependentSQLPlanner(model)(CTX)
    assert len(model.calls)==1


def test_alias_prompt_distinguishes_source_from_output():
    model=SequenceModel([GOOD])
    IndependentSQLPlanner(model)(CTX)
    assert 'alias need not exist in the source schema' in model.calls[0]


def test_pipeline_persists_exhaustion_and_does_not_release(tmp_path,monkeypatch):
    from sql_agent.pipeline import JobPipeline
    from sql_agent.sql_config import SQLTask,SQLTaskRegistry
    from sql_agent.jobs import SqliteJobRepository
    monkeypatch.setattr('sql_agent.model_recovery.time.sleep',lambda _:None)
    primary=SequenceModel(['{"action":"REPAIR","sql":"SELECT name FROM employees ORDER BY id"}']*2)
    checker=SequenceModel([TimeoutError('down')]*6)
    planner=SQLAgentPlanner(primary)
    reviewer=IndependentSQLPlanner(checker)
    pipeline=JobPipeline(SQLTaskRegistry((SQLTask('names','List names ordered by id',DataContract(('name',))),)),planner,reviewer=reviewer)
    repo=SqliteJobRepository(tmp_path/'jobs.sqlite')
    for i in range(2):
        job,_=repo.submit('sql_analysis',{'task_id':'names'},str(i))
        result=pipeline.run(job)
        assert result.status=='needs_review'
        assert result.result['output'] is None
        assert result.result['release']['approved'] is False
        assert result.result['routing']['reason']=='semantic_check_unavailable'
        events=result.result['model_recovery']['checker']
        assert len([e for e in events if e['event']=='failure'])==3
        assert events[-1]['retry'] is False
        assert result.result['telemetry']['stages']['checker']['calls'] == 3
        assert result.result['telemetry']['stages']['checker']['retries'] == 2
        assert not reviewer.recovery_events and not planner.recovery_events
    assert len(checker.calls)==6
