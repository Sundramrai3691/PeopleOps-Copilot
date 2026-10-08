"""Clarification lifecycle observed through repository and HTTP interfaces."""
import pytest
import os
import uuid

from sql_agent.jobs import SqliteJobRepository


def test_api_resumes_only_for_owner_or_admin(tmp_path):
    from fastapi.testclient import TestClient
    from sql_agent.api import create_app
    from sql_agent.security import ApiKeyAuthorizer, Principal
    from sql_agent.sql_config import SQLTaskRegistry
    repo = SqliteJobRepository(tmp_path / 'jobs.db')
    keys = ApiKeyAuthorizer({name: Principal(name, role) for name, role in
                            [('alice','operator'), ('bob','operator'), ('admin','admin')]})
    client = TestClient(create_app(repo, SQLTaskRegistry(), keys))
    job, _ = repo.submit('sql_request', {'task_id':'employee_names', '_submitted_by':'alice'}, 'request')
    claim = repo.claim_next()
    repo.finish(job.job_id, 'waiting_user', {'clarification':{'id':'q1','question':'Which employees?'}}, claim=claim)
    url = '/v1/requests/' + job.job_id + '/resume'
    body = {'clarification_id':'q1', 'answer':'All employees'}
    assert client.post(url, json=body, headers={'x-api-key':'bob','idempotency-key':'reply'}).status_code == 403
    r = client.post(url, json=body, headers={'x-api-key':'alice','idempotency-key':'reply'})
    assert r.status_code == 202, r.text
    assert r.json()['job']['status'] == 'queued'
    assert client.post(url, json=body, headers={'x-api-key':'alice','idempotency-key':'reply'}).status_code == 202
    assert client.post(url, json={**body,'answer':'Different'}, headers={'x-api-key':'alice','idempotency-key':'reply'}).status_code == 409


@pytest.fixture(params=['sqlite', 'postgresql'])
def repository_factory(request, tmp_path):
    if request.param == 'sqlite':
        yield lambda: SqliteJobRepository(tmp_path / 'jobs.db')
        return
    dsn = os.environ.get('SQL_AGENT_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('disposable PostgreSQL DSN required')
    import psycopg
    from psycopg.conninfo import make_conninfo
    from sql_agent.postgres_jobs import PostgresJobRepository
    schema = 'job_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(psycopg.sql.SQL('CREATE SCHEMA {}').format(psycopg.sql.Identifier(schema)))
    isolated = make_conninfo(dsn, options='-c search_path=' + schema)
    try:
        yield lambda: PostgresJobRepository(isolated)
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute(psycopg.sql.SQL('DROP SCHEMA {} CASCADE').format(psycopg.sql.Identifier(schema)))


def test_waiting_job_survives_restart_and_duplicate_answer(repository_factory):
    repo = repository_factory()
    job, _ = repo.submit('sql_request', {'database_id':'demo', '_submitted_by':'alice'}, 'request')
    claim = repo.claim_next('worker')
    repo.finish(job.job_id, 'waiting_user', {
        'clarification': {'id':'question-1', 'question':'Which order?'}}, claim=claim)
    restarted = repository_factory()
    assert restarted.get(job.job_id).status == 'waiting_user'
    assert restarted.claim_next('other') is None
    resumed = restarted.resume(job.job_id, 'question-1', 'Order 1', 'alice', 'answer-1')
    assert resumed.status == 'queued'
    assert resumed.attempts == 1
    replacement = restarted.claim_next('worker-2')
    duplicate = restarted.resume(job.job_id, 'question-1', 'Order 1', 'alice', 'answer-1')
    assert duplicate.status == 'running'
    assert replacement.attempts == 2
    assert len(duplicate.payload['_clarifications']) == 1
    with pytest.raises(RuntimeError):
        restarted.finish(job.job_id, 'completed', {'stale':True}, claim=claim)
    with pytest.raises(ValueError):
        restarted.resume(job.job_id, 'question-1', 'Order 2', 'alice', 'answer-1')
    assert len([e for e in restarted.events(job.job_id) if e['event_type'] == 'user_resumed']) == 1


def test_retry_token_ledger_survives_repository_restart(repository_factory):
    from sql_agent.worker import Worker
    from sql_agent.pipeline import PipelineOutcome
    from sql_agent.telemetry import summarize_calls
    repo = repository_factory()
    job, _ = repo.submit('sql_request', {}, 'token-retry')
    def usage():
        return summarize_calls({'planner':[{'event':'failure','attempt':1,
            'prompt_tokens':8,'completion_tokens':2}]}, 1)
    class Interrupted:
        def run(self, claimed):
            error = TimeoutError('transport interrupted after usage observation')
            error.pipeline_evidence = {'telemetry':usage()}
            raise error
    Worker(repo, Interrupted()).run_once()
    assert repo.get(job.job_id).status == 'queued'
    class Recovered:
        def run(self, claimed):
            return PipelineOutcome('needs_review', {'telemetry':usage()})
    repo = repository_factory()
    Worker(repo, Recovered()).run_once()
    result = repo.get(job.job_id).result
    assert result['telemetry']['token_cost']['total_tokens'] == 20
    assert result['attempt_telemetry']['token_cost']['total_tokens'] == 10
    assert result['telemetry']['unobserved_job_attempts'] == 0
