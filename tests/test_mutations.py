import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from sql_agent.api import create_app
from sql_agent.jobs import SqliteJobRepository
from sql_agent.mutations import MutationPolicy, MutationService
from sql_agent.security import ApiKeyAuthorizer, Principal
from sql_agent.sql_config import SQLTaskRegistry


@pytest.fixture
def changes(tmp_path):
    path = tmp_path / "business.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY, amount INTEGER NOT NULL)")
        conn.executemany("INSERT INTO orders VALUES (?,?)", [(1, 10), (2, 20)])
        conn.execute("CREATE TABLE private_data(secret TEXT)")
    policy = MutationPolicy(path, ("orders", "new_table"), allow_ddl=True, max_rows=2)
    return MutationService(tmp_path / "control.db", {"demo": policy})


def approve(service, proposal):
    return service.review(proposal["id"], proposal["proposal_sha256"], "human", "approve")


@pytest.mark.parametrize("sql,expected", [
    ("INSERT INTO orders VALUES (3, 30)", [[1, 10], [2, 20], [3, 30]]),
    ("UPDATE orders SET amount=11 WHERE id=1", [[1, 11], [2, 20]]),
    ("DELETE FROM orders WHERE id=1", [[2, 20]]),
])
def test_dml_preview_does_not_write_and_approval_executes_once(changes, sql, expected):
    p = changes.propose("demo", sql, "operator")
    assert changes.query("demo", "SELECT * FROM orders ORDER BY id")["rows"] == [(1, 10), (2, 20)]
    result = approve(changes, p)
    restarted = MutationService(changes.store, changes.policies)
    assert approve(restarted, p) == result
    with sqlite3.connect(changes.policies["demo"].database) as conn:
        assert [list(r) for r in conn.execute("SELECT * FROM orders ORDER BY id")] == expected
    assert restarted.inspect(p["id"])["status"] == "completed"


def test_create_drop(changes):
    p = changes.propose("demo", "CREATE TABLE new_table(id INTEGER PRIMARY KEY, label TEXT)", "operator")
    approve(changes, p)
    approve(changes, changes.propose("demo", "INSERT INTO new_table VALUES (1, 'test')", "operator"))
    p = changes.propose("demo", "DROP TABLE new_table", "operator")
    assert p["affected_rows"] == 1
    approve(changes, p)
    with sqlite3.connect(changes.policies["demo"].database) as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='new_table'").fetchall()


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders", "UPDATE orders SET amount=0", "DROP TABLE private_data",
    "INSERT INTO orders VALUES (3,30); DROP TABLE orders", "PRAGMA writable_schema=ON",
    "ATTACH DATABASE '/tmp/other.db' AS other", "CREATE TRIGGER t AFTER INSERT ON orders BEGIN DELETE FROM orders; END",
    "CREATE VIEW new_table AS SELECT * FROM orders", "CREATE VIRTUAL TABLE new_table USING fts5(x)",
    "INSERT INTO orders VALUES (3,30),(4,40),(5,50)",
    "INSERT OR REPLACE INTO orders VALUES (1,50)",
    "UPDATE orders SET amount=random() WHERE id=1",
    "DELETE FROM _sql_agent_mutation_receipts WHERE 1=1",
    "INSERT INTO orders SELECT 3, length(secret) FROM private_data",
])
def test_unsafe_or_unsupported_sql_rejected(changes, sql):
    with pytest.raises((ValueError, sqlite3.Error)):
        changes.propose("demo", sql, "operator")
    assert changes.query("demo", "SELECT * FROM orders ORDER BY id")["rows"] == [(1, 10), (2, 20)]


def test_stale_preview_rejected(changes):
    p = changes.propose("demo", "DELETE FROM orders WHERE id=1", "operator")
    with sqlite3.connect(changes.policies["demo"].database) as conn:
        conn.execute("UPDATE orders SET amount=12 WHERE id=1")
    with pytest.raises(ValueError, match="changed since preview"):
        approve(changes, p)
    assert changes.query("demo", "SELECT amount FROM orders WHERE id=1")["rows"] == [(12,)]


def test_hash_rejection_expiry_and_policy(changes, monkeypatch):
    p = changes.propose("demo", "DELETE FROM orders WHERE id=1", "operator")
    with pytest.raises(ValueError, match="hash"):
        changes.review(p["id"], "0" * 64, "human", "approve")
    monkeypatch.setattr("sql_agent.mutations.time.time", lambda: p["expires_at"] + 1)
    with pytest.raises(ValueError, match="expired"):
        approve(changes, p)


def test_reject_is_terminal(changes):
    p = changes.propose("demo", "DELETE FROM orders WHERE id=1", "operator")
    changes.review(p["id"], p["proposal_sha256"], "human", "reject")
    with pytest.raises(ValueError, match="already reviewed"):
        approve(changes, p)


def test_concurrent_approval(changes):
    p = changes.propose("demo", "UPDATE orders SET amount=amount+1 WHERE id=1", "operator")
    def attempt(_):
        try:
            return approve(changes, p)
        except ValueError as exc:
            assert 'already executing' in str(exc)
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [r for r in pool.map(attempt, range(2)) if r is not None]
    assert results and all(r == approve(changes, p) for r in results)
    assert changes.query("demo", "SELECT amount FROM orders WHERE id=1")["rows"] == [(11,)]


def test_trigger_side_effect_denied(changes):
    with sqlite3.connect(changes.policies["demo"].database) as conn:
        conn.execute("CREATE TRIGGER leak AFTER UPDATE ON orders BEGIN INSERT INTO private_data VALUES ('x'); END")
    with pytest.raises(sqlite3.Error):
        changes.propose("demo", "UPDATE orders SET amount=15 WHERE id=1", "operator")


def test_api_roles_and_disabled_default(changes, tmp_path, monkeypatch):
    monkeypatch.delenv("SQL_AGENT_MUTATION_CONFIG", raising=False)
    keys = ApiKeyAuthorizer({
        "viewer": Principal("analyst", "viewer"),
        "operator": Principal("analyst", "operator"),
        "admin": Principal("administrator", "admin"),
    })
    args = (SqliteJobRepository(tmp_path / "jobs.db"), SQLTaskRegistry(), keys)
    payload = {"database_id": "demo", "sql": "DELETE FROM orders WHERE id=1"}
    with TestClient(create_app(*args)) as client:
        assert client.post("/v1/mutations", json=payload, headers={"x-api-key": "operator"}).status_code == 403
    with TestClient(create_app(*args, mutations=changes)) as client:
        assert client.post("/v1/mutations", json=payload, headers={"x-api-key": "viewer"}).status_code == 403
        r = client.post("/v1/mutations", json=payload, headers={"x-api-key": "operator"})
        assert r.status_code == 201, r.text
        p = r.json()
        endpoint = f'/v1/mutations/{p["id"]}/review'
        review = {"proposal_sha256": p["proposal_sha256"], "decision": "approve"}
        assert client.post(endpoint, json=review, headers={"x-api-key": "operator"}).status_code == 403
        r = client.post(endpoint, json=review, headers={"x-api-key": "admin"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "completed"


def test_query_read_only(changes):
    for sql in ["DELETE FROM orders WHERE id=1", "SELECT * FROM private_data", "SELECT load_extension('x')"]:
        with pytest.raises((ValueError, sqlite3.Error)):
            changes.query("demo", sql)


def test_langgraph_pauses_and_recovers_after_committed_response_loss(changes, monkeypatch):
    p = changes.propose('demo', 'UPDATE orders SET amount=amount+1 WHERE id=1', 'operator')
    with changes.workflow.session(p['id']) as (graph, config):
        state = graph.get_state(config)
        assert state.next == ('await_approval',)
        assert any(t.interrupts for t in state.tasks)
    original = changes._review
    def lose_response(*args):
        original(*args)
        raise RuntimeError('simulated lost response after business commit')
    monkeypatch.setattr(changes, '_review', lose_response)
    with pytest.raises(RuntimeError, match='lost response'):
        approve(changes, p)
    assert changes.inspect(p['id'])['status'] == 'completed'
    restarted = MutationService(changes.store, changes.policies)
    assert approve(restarted, p)['status'] == 'completed'
    assert restarted.query('demo', 'SELECT amount FROM orders WHERE id=1')['rows'] == [(11,)]


def test_disabled_ddl_and_policy_drift(changes):
    from dataclasses import replace
    p = changes.propose('demo', 'DELETE FROM orders WHERE id=1', 'operator')
    changes.policies['demo'] = replace(changes.policies['demo'], allow_ddl=False)
    with pytest.raises(ValueError, match='policy changed'):
        approve(changes, p)
    with pytest.raises(ValueError, match='permission'):
        changes.propose('demo', 'DROP TABLE orders', 'operator')


def test_mcp_can_propose_but_cannot_approve(monkeypatch):
    from sql_agent import mcp_server
    calls = []
    monkeypatch.setattr(mcp_server, 'api_call', lambda *args: calls.append(args) or {'status': 'needs_approval'})
    response = mcp_server.dispatch({'id': 1, 'method': 'tools/call', 'params': {
        'name': 'propose_database_change', 'arguments': {'database_id': 'demo', 'sql': 'DELETE FROM orders WHERE id=1'}}})
    assert not response['result']['isError']
    assert calls[0][0] == '/v1/mutations'
    assert not any('approve' in tool['name'] for tool in mcp_server.TOOL_SCHEMAS)


def test_database_console_real_api(changes, tmp_path):
    import shutil
    from urllib.parse import urlsplit
    playwright = pytest.importorskip('playwright.sync_api')
    chrome = shutil.which('google-chrome')
    if not chrome:
        pytest.skip('Chrome not installed')
    keys = ApiKeyAuthorizer({'admin': Principal('human', 'admin')})
    app = create_app(SqliteJobRepository(tmp_path / 'jobs.db'), SQLTaskRegistry(), keys, changes)
    from sql_agent.pipeline import JobPipeline
    from sql_agent.sql_config import DemoSQLPlanner
    from sql_agent.worker import Worker
    worker = Worker(SqliteJobRepository(tmp_path / 'jobs.db'), JobPipeline(SQLTaskRegistry(), DemoSQLPlanner(), mutations=changes))
    with TestClient(app) as client, playwright.sync_playwright() as p:
        assert client.post('/v1/auth/login', json={'api_key': 'admin'}).status_code == 200
        browser = p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1100, 'height': 1000})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        def route(request):
            path = urlsplit(request.request.url).path
            response = client.request(request.request.method, path,
                                      content=request.request.post_data,
                                      headers={'content-type': 'application/json'})
            if path == '/v1/requests' and request.request.method == 'POST':
                # Forward the browser's idempotency header for the real API.
                response = client.post(path, content=request.request.post_data,
                    headers={'content-type': 'application/json', 'idempotency-key': request.request.headers['idempotency-key']})
                worker.run_once()
            request.fulfill(status=response.status_code, body=response.content,
                            content_type=response.headers.get('content-type', 'application/json'))
        page.route('**/*', route)
        page.goto('http://127.0.0.1:49998/database')
        page.wait_for_function("document.querySelector('#source').options.length === 2")
        page.select_option('#source', 'demo')
        page.select_option('#input-kind', 'sql')
        page.fill('#prompt', 'UPDATE orders SET amount=11 WHERE id=1')
        page.click('#submit')
        page.wait_for_function("document.querySelector('#status').textContent === 'needs_review'")
        assert changes.query('demo', 'SELECT amount FROM orders WHERE id=1')['rows'] == [(10,)]
        page.on('dialog', lambda d: d.accept())
        page.click('#approve')
        page.wait_for_function("document.querySelector('#status').textContent === 'review_approved'")
        assert changes.query('demo', 'SELECT amount FROM orders WHERE id=1')['rows'] == [(11,)]
        page.screenshot(path='/tmp/sql-agent-unified-console-20260909.png', full_page=True)
        assert not errors
        browser.close()
