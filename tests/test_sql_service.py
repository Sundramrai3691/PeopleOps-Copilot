import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from sql_agent.api import create_app
from sql_agent.jobs import SqliteJobRepository
from sql_agent.pipeline import JobPipeline
from sql_agent.security import ApiKeyAuthorizer, Principal
from sql_agent.sql_config import DemoSQLPlanner, SQLTaskRegistry
from sql_agent.worker import Worker
from sql_agent import mcp_server


@pytest.fixture
def service(tmp_path):
    repository = SqliteJobRepository(tmp_path / "jobs.db")
    registry = SQLTaskRegistry()
    authorizer = ApiKeyAuthorizer({"read": Principal("analyst", "viewer"),
                                  "write": Principal("analyst", "operator"),
                                  "admin": Principal("admin", "admin")})
    client = TestClient(create_app(repository, registry, authorizer))
    worker = Worker(repository, JobPipeline(registry, DemoSQLPlanner()))
    return client, repository, worker


def headers(key="write", ident="request-1"):
    return {"x-api-key": key, "idempotency-key": ident}


def test_no_unmeasured_benchmark_is_published(service):
    client, _, _ = service
    assert client.get('/benchmark').status_code == 404
    assert 'href="/benchmark"' not in client.get('/').text


def test_api_worker_repair_evidence_and_replay(service):
    client, repository, worker = service
    payload = {"task_id": "employee_names", "initial_sql": "SELECT missing FROM employees"}
    response = client.post("/v1/jobs", json=payload, headers=headers())
    assert response.status_code == 202, response.text
    original = response.json()["job"]
    duplicate = client.post("/v1/jobs", json=payload, headers=headers())
    assert duplicate.json()["created"] is False
    assert duplicate.json()["job"]["job_id"] == original["job_id"]
    changed = client.post("/v1/jobs", json={**payload, "initial_sql": "SELECT name FROM employees"}, headers=headers())
    assert changed.status_code == 409
    assert worker.run_once()
    result = client.get('/v1/jobs/' + original["job_id"], headers=headers("read")).json()
    assert result["status"] == "completed"
    assert result["result"]["output"]["rows"] == [["Ada"], ["Grace"], ["Linus"]]
    record = result["result"]["execution_record"]
    assert record["budgets"]["agent_repairs"] == 1
    assert record["contract"]["domain"] == "sql_query"
    replay = client.post('/v1/jobs/' + original["job_id"] + '/replay', headers=headers(ident="replay"))
    assert replay.status_code == 202
    assert worker.run_once()
    replayed = repository.get(replay.json()["job"]["job_id"])
    assert replayed.status == "completed"
    assert replayed.payload["_execution_contract_sha256"] == record["contract"]["sha256"]
    trace = client.get('/v1/traces/' + original["payload"]["_trace_id"], headers=headers("read"))
    assert len(trace.json()["runs"]) == 2


def test_only_sql_surface_and_registered_contracts(service):
    client, _, _ = service
    assert client.get('/health').json()["service"] == "peopleops_copilot"
    assert client.get('/v1/tasks').status_code == 401
    assert client.get('/v1/tasks', headers=headers("read")).json()["tasks"][0]["task_id"] == "employee_names"
    assert client.post('/v1/jobs', json={"task_id": "employee_names"}, headers=headers("read")).status_code == 403
    assert client.post('/v1/jobs', json={"task_id": "other"}, headers=headers()).status_code == 422
    assert client.post('/v1/jobs', json={"task_id": "employee_names", "database": "/tmp/secret"}, headers=headers()).status_code == 422
    paths = client.get('/openapi.json').json()["paths"]
    assert not any(p in paths for p in ['/v1/uploads', '/v1/analyze', '/v1/cases', '/v1/protocols'])


def test_unsafe_sql_routes_to_review_without_execution(service):
    client, repo, worker = service
    job = client.post('/v1/jobs', json={"task_id": "employee_names", "initial_sql": "DROP TABLE employees"}, headers=headers()).json()["job"]
    worker.run_once()
    stored = repo.get(job["job_id"])
    assert stored.status == "needs_review"
    assert stored.result["output"] is None
    record = stored.result["execution_record"]
    approved = client.post('/v1/jobs/' + job['job_id'] + '/review', json={"decision": "approve", "notes": "Reviewed rejection"}, headers=headers("admin"))
    assert approved.status_code == 200
    assert approved.json()["result"]["output"] is None
    assert approved.json()["result"]["execution_record"] == record


def test_contract_change_rejected_before_query(service):
    client, repo, worker = service
    job, _ = repo.submit("sql_analysis", {"task_id": "employee_names", "_execution_contract_sha256": "wrong"}, "drift")
    worker.run_once()
    assert repo.get(job.job_id).status == "failed"
    assert "contract differs" in repo.get(job.job_id).error["message"]


def test_registered_database_opens_read_only(tmp_path, monkeypatch):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE items(name TEXT)")
    config = tmp_path / "tasks.json"
    config.write_text(json.dumps([{"task_id": "items", "question": "List items", "database": "source.db", "contract": {"columns": ["name"]}}]))
    monkeypatch.setenv("SQL_AGENT_SQL_TASKS", str(config))
    registry = SQLTaskRegistry.from_env()
    class Job:
        payload = {"task_id": "items"}
    session = registry.session(Job())
    try:
        with pytest.raises(sqlite3.OperationalError):
            session.connection.execute("DELETE FROM items")
    finally:
        session.connection.close()


def test_mcp_sql_tools_forward_to_authenticated_api(service, monkeypatch):
    client, _, worker = service
    def call(path, payload=None, idempotency_key=None):
        response = client.get(path, headers=headers()) if payload is None else client.post(path, json=payload, headers=headers(ident=idempotency_key))
        assert response.is_success, response.text
        return response.json()
    monkeypatch.setattr(mcp_server, "api_call", call)
    schemas = mcp_server.dispatch({"id": 1, "method": "tools/list"})["result"]["tools"]
    assert {s["name"] for s in schemas} == {"list_sql_tasks", "submit_sql_task", "get_sql_job",
        "list_databases", "query_database", "propose_database_change", "get_database_change",
        "list_sql_sources", "submit_sql_request", "get_sql_request", "get_employee_profile",
        "get_leave_balance", "get_team_headcount", "get_attendance_summary",
        "create_leave_request", "ask_hr_policy"}
    submitted = mcp_server.dispatch({"id": 2, "method": "tools/call", "params": {
        "name": "submit_sql_task", "arguments": {"task_id": "employee_names", "idempotency_key": "mcp"}}})
    job_id = submitted["result"]["structuredContent"]["job"]["job_id"]
    worker.run_once()
    read = mcp_server.dispatch({"id": 3, "method": "tools/call", "params": {
        "name": "get_sql_job", "arguments": {"job_id": job_id}}})
    assert read["result"]["structuredContent"]["status"] == "completed"


def test_browser_login_session_refresh_and_logout(service):
    client, _, _ = service
    assert client.get('/v1/auth/session').status_code == 401
    assert client.post('/v1/auth/login', json={'api_key': 'wrong'}).status_code == 401
    signed_in = client.post('/v1/auth/login', json={'api_key': 'admin'})
    assert signed_in.status_code == 200
    assert signed_in.json() == {'name': 'admin', 'role': 'admin'}
    cookie = signed_in.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie
    token = client.cookies.get('peopleops_session')
    assert client.get('/v1/auth/session').json()['role'] == 'admin'
    assert client.get('/v1/tasks').status_code == 200
    assert client.post('/v1/auth/logout').status_code == 200
    assert client.get('/v1/tasks').status_code == 401
    client.cookies.set('peopleops_session', token)
    assert client.get('/v1/tasks').status_code == 401


def test_browser_session_preserves_roles_and_api_key_clients(service):
    client, _, _ = service
    assert client.post('/v1/auth/login', json={'api_key': 'read'}).status_code == 200
    assert client.get('/v1/tasks').status_code == 200
    response = client.post('/v1/jobs', json={'task_id':'employee_names'}, headers={'idempotency-key':'viewer-test'})
    assert response.status_code == 403
    assert client.get('/v1/tasks', headers=headers('write')).status_code == 200


def test_cookie_authenticated_cross_origin_mutation_is_rejected(service):
    client, _, _ = service
    assert client.post('/v1/auth/login', json={'api_key':'admin'}).status_code == 200
    response=client.post('/v1/auth/logout', headers={'origin':'https://different.example'})
    assert response.status_code == 403
    assert client.get('/v1/auth/session').status_code == 200
