from sql_agent.bounded_runtime import ActionProposal
from sql_agent.jobs import SqliteJobRepository
from sql_agent.pipeline import JobPipeline
from sql_agent.sql_config import SQLTaskRegistry, SQLTask
from sql_agent.data_agent import DataContract


def test_executable_wrong_answer_is_not_published_as_completed(tmp_path):
    repo = SqliteJobRepository(tmp_path / 'jobs.sqlite')
    job, _ = repo.submit('sql_analysis', {'task_id': 'employee_names'}, 'wrong-result')
    planner = lambda ctx: ActionProposal('REPAIR', 'sql_query',
                                         {'sql': 'SELECT department AS name FROM employees'})
    result = JobPipeline(SQLTaskRegistry((SQLTask('employee_names', 'List employee names', DataContract(('name',))),)), planner).run(job)
    assert result.status == 'needs_review'
    assert result.result['output'] is None

import sqlite3
import pytest
from sql_agent.data_agent import DataAgentLoop
from sql_agent.semantic_review import SemanticSQLSession, IndependentSQLPlanner
from sql_agent.sql_environment import demo_database


def proposal(sql):
    return ActionProposal('REPAIR', 'sql_query', {'sql': sql})


def test_disagreement_repairs_without_exposing_check_sql():
    db = demo_database()
    checks = []
    def reviewer(ctx):
        checks.append(ctx)
        assert not ctx.evidence.previous_sql and not ctx.evidence.previous_error
        assert not ctx.initial_sql
        return proposal('SELECT name AS independently_named FROM employees ORDER BY id')
    def planner(ctx):
        if ctx.attempt == 1:
            return proposal('SELECT department AS name FROM employees ORDER BY id')
        assert ctx.evidence.previous_reason == 'semantic_query_disagreement'
        assert 'independently_named' not in str(ctx)
        return proposal('SELECT name FROM employees ORDER BY id')
    session = SemanticSQLSession(db, DataContract(('name',)), 'List names', reviewer)
    try:
        result = DataAgentLoop(3).run('List names', planner, session)
        assert result.decision == 'KEEP'
        assert result.reason == 'independent_query_agreement_not_semantic_proof'
        assert len(checks) == 1
        assert session.semantic_review['proof'] is False
        assert session.last_sql == 'SELECT name FROM employees ORDER BY id'
    finally:
        db.close()


@pytest.mark.parametrize('check', [
    'DELETE FROM employees', 'SELECT load_extension(\'x\')',
    'SELECT missing FROM employees', 'SELECT name FROM employees UNION ALL SELECT name FROM employees'])
def test_unavailable_or_unsafe_check_fails_closed(check):
    db = demo_database()
    try:
        session = SemanticSQLSession(db, DataContract(('name',), max_rows=3),
            'List names', lambda ctx: proposal(check))
        result = DataAgentLoop().run('List names', lambda ctx: proposal('SELECT name FROM employees'), session)
        assert result.decision == 'STOP'
        assert result.reason == 'semantic_check_unavailable'
        assert db.execute('SELECT count(*) FROM employees').fetchone()[0] == 3
    finally:
        db.close()


@pytest.mark.parametrize('check', [
    'SELECT name FROM employees ORDER BY id DESC',
    'SELECT name FROM employees UNION ALL SELECT name FROM employees',
    'SELECT name, department FROM employees'])
def test_comparison_preserves_order_duplicates_and_column_count(check):
    db = demo_database()
    try:
        session = SemanticSQLSession(db, DataContract(('name',)), 'Names', lambda ctx: proposal(check))
        result = DataAgentLoop(1).run('Names', lambda ctx: proposal('SELECT name FROM employees ORDER BY id'), session)
        assert result.decision == 'STOP'
        assert session.semantic_review['status'] == 'disagreement'
    finally:
        db.close()


def test_agreement_does_not_upgrade_to_proof_or_auto_publish(tmp_path):
    repo = SqliteJobRepository(tmp_path / 'jobs.sqlite')
    job, _ = repo.submit('sql_analysis', {'task_id': 'names'}, 'agreement')
    registry = SQLTaskRegistry((SQLTask('names', 'List names', DataContract(('name',))),))
    same_wrong = lambda ctx: proposal('SELECT department AS name FROM employees')
    result = JobPipeline(registry, same_wrong, reviewer=same_wrong).run(job)
    assert result.status == 'needs_review'
    assert result.result['output'] is None
    assert result.result['candidate_output']['rows'][0] == ('AI',)
    assert result.result['semantic_review']['status'] == 'agreement'
    assert result.result['semantic_review']['proof'] is False


def test_same_snapshot_for_primary_and_check(tmp_path):
    path = tmp_path / 'source.sqlite'
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('CREATE TABLE items(x); INSERT INTO items VALUES (1);')
    def reviewer(ctx):
        with sqlite3.connect(path) as other:
            other.execute('INSERT INTO items VALUES (2)')
        return proposal('SELECT x FROM items')
    try:
        session = SemanticSQLSession(db, DataContract(('x',)), 'List x', reviewer)
        result = DataAgentLoop(1).run('List x', lambda ctx: proposal('SELECT x FROM items'), session)
        assert result.decision == 'KEEP'
        assert result.output['rows'] == ((1,),)
    finally:
        db.close()


def test_independent_prompt_excludes_candidate_and_reference():
    from sql_agent.data_agent import PlanningContext, SQLPlanningEvidence
    class Model:
        def complete(self, prompt):
            assert 'secret_candidate' not in prompt
            assert 'secret_gold' not in prompt
            assert 'secret_error' not in prompt
            return '{"action":"STOP"}'
    context = PlanningContext('Names', DataContract(('name',), verification_sql='secret_gold'),
        SQLPlanningEvidence((('employees', 'CREATE TABLE employees(name)'),), 'hash',
                            previous_sql='secret_candidate', previous_error='secret_error'), 2, 1, 'secret_candidate')
    assert IndependentSQLPlanner(Model())(context).action == 'STOP'


def test_api_worker_persists_review_evidence_without_releasing_wrong_rows(tmp_path):
    from fastapi.testclient import TestClient
    from sql_agent.api import create_app
    from sql_agent.security import ApiKeyAuthorizer, Principal
    from sql_agent.worker import Worker
    repo = SqliteJobRepository(tmp_path / 'jobs.sqlite')
    registry = SQLTaskRegistry((SQLTask('names', 'List names', DataContract(('name',))),))
    authorizer = ApiKeyAuthorizer({'test-key': Principal('test-operator', 'operator')})
    wrong = lambda ctx: proposal('SELECT department AS name FROM employees')
    worker = Worker(repo, JobPipeline(registry, wrong, reviewer=wrong))
    with TestClient(create_app(repo, registry, authorizer)) as client:
        headers = {'x-api-key': 'test-key', 'idempotency-key': 'semantics'}
        response = client.post('/v1/jobs', json={'task_id': 'names'}, headers=headers)
        assert response.status_code == 202
        job_id = response.json()['job']['job_id']
        assert worker.run_once()
        result = client.get('/v1/jobs/' + job_id, headers=headers).json()
        assert result['status'] == 'needs_review'
        assert result['result']['output'] is None
        assert result['result']['candidate_output']['rows'][0] == ['AI']
        assert result['result']['release']['approved'] is False
        assert result['result']['semantic_review']['status'] == 'agreement'
        assert result['result']['semantic_review']['proof'] is False


def test_reviewer_timeout_is_not_a_repair_opportunity():
    calls = []
    def reviewer(ctx):
        calls.append(ctx)
        raise TimeoutError('injected unavailable check')
    db = demo_database()
    try:
        session = SemanticSQLSession(db, DataContract(('name',)), 'Names', reviewer)
        result = DataAgentLoop(3).run('Names', lambda ctx: proposal('SELECT name FROM employees'), session)
        assert result.decision == 'STOP'
        assert result.reason == 'semantic_check_unavailable'
        assert len(calls) == 1
    finally:
        db.close()


def test_independent_planner_receives_application_output_contract():
    import json
    from sql_agent.data_agent import PlanningContext, SQLPlanningEvidence
    class Model:
        def complete(self, prompt):
            payload = json.loads(prompt.split('\n', 1)[1])
            assert payload['required_output_columns'] == ['item']
            return '{"action":"REPAIR","sql":"SELECT name AS item FROM employees"}'
    context = PlanningContext('List names', DataContract(('item',)),
        SQLPlanningEvidence((('employees', 'CREATE TABLE employees(id, name)'),), 'hash'), 1, 0)
    IndependentSQLPlanner(Model())(context)
