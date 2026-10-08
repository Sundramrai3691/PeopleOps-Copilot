"""Opt-in live integration tests. DSN must point to a disposable test database."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from sql_agent.mutations import MutationService
from sql_agent.postgres_mutations import PostgresPolicy


@pytest.fixture
def pg_changes(tmp_path):
    if not os.environ.get('SQL_AGENT_TEST_POSTGRES_DSN'):
        pytest.skip('SQL_AGENT_TEST_POSTGRES_DSN not set (disposable database required)')
    psycopg = pytest.importorskip('psycopg')
    schema = 'crud_test_' + uuid.uuid4().hex
    with psycopg.connect(os.environ['SQL_AGENT_TEST_POSTGRES_DSN']) as conn:
        conn.execute(psycopg.sql.SQL('CREATE SCHEMA {}').format(psycopg.sql.Identifier(schema)))
        conn.execute(psycopg.sql.SQL('CREATE TABLE {}.orders(id INTEGER PRIMARY KEY, amount INTEGER NOT NULL)').format(psycopg.sql.Identifier(schema)))
        conn.execute(psycopg.sql.SQL('INSERT INTO {}.orders VALUES (1,10),(2,20)').format(psycopg.sql.Identifier(schema)))
    policy = PostgresPolicy('SQL_AGENT_TEST_POSTGRES_DSN', schema, ('orders', 'new_table'), allow_ddl=True, max_rows=2)
    try:
        yield MutationService(tmp_path / 'control.db', {'demo': policy})
    finally:
        with psycopg.connect(os.environ['SQL_AGENT_TEST_POSTGRES_DSN']) as conn:
            conn.execute(psycopg.sql.SQL('DROP SCHEMA {} CASCADE').format(psycopg.sql.Identifier(schema)))


def approve(service, proposal):
    return service.review(proposal['id'], proposal['proposal_sha256'], 'human', 'approve')


def test_read_role_enforces_row_isolation_and_denies_writes(pg_changes, monkeypatch, tmp_path):
    import secrets
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    policy = pg_changes.policies['demo']
    role = 'reader_' + uuid.uuid4().hex
    password = secrets.token_hex(16)
    with psycopg.connect(os.environ['SQL_AGENT_TEST_POSTGRES_DSN']) as conn:
        conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
        conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(policy.schema),sql.Identifier(role)))
        conn.execute(sql.SQL('GRANT SELECT ON {}.orders TO {}').format(sql.Identifier(policy.schema),sql.Identifier(role)))
        conn.execute(sql.SQL('ALTER TABLE {}.orders ENABLE ROW LEVEL SECURITY').format(sql.Identifier(policy.schema)))
        conn.execute(sql.SQL('CREATE POLICY visible_rows ON {}.orders FOR SELECT TO {} USING (id=1)').format(
            sql.Identifier(policy.schema),sql.Identifier(role)))
    values = conninfo_to_dict(os.environ['SQL_AGENT_TEST_POSTGRES_DSN'])
    values.update(user=role,password=password)
    read_dsn=make_conninfo(**values)
    monkeypatch.setenv('SQL_AGENT_TEST_READ_DSN',read_dsn)
    read_policy=PostgresPolicy('SQL_AGENT_TEST_READ_DSN',policy.schema,('orders',))
    service=MutationService(tmp_path/'read-control.db',{'read':read_policy})
    try:
        assert service.query('read','SELECT id, amount FROM orders ORDER BY id')['rows'] == [(1,10)]
        # Reading through RLS does not authorize the mutation snapshot profile.
        with pytest.raises(ValueError, match='non-RLS'):
            pg_changes.propose('demo', 'UPDATE orders SET amount=0 WHERE id=1', 'operator')
        with psycopg.connect(read_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql.SQL('UPDATE {}.orders SET amount=0').format(sql.Identifier(policy.schema)))
    finally:
        with psycopg.connect(os.environ['SQL_AGENT_TEST_POSTGRES_DSN']) as conn:
            conn.execute(sql.SQL('DROP POLICY visible_rows ON {}.orders').format(sql.Identifier(policy.schema)))
            conn.execute(sql.SQL('REVOKE ALL ON {}.orders FROM {}').format(sql.Identifier(policy.schema),sql.Identifier(role)))
            conn.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM {}').format(sql.Identifier(policy.schema),sql.Identifier(role)))
            conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@pytest.mark.parametrize('sql,rows', [
    ('INSERT INTO orders VALUES (3,30)', [(1,10),(2,20)]),
    ('UPDATE orders SET amount=11 WHERE id=1', [(1,11),(2,20)]),
    ('DELETE FROM orders WHERE id=1', [(2,20)]),
])
def test_live_crud_and_duplicate_approval(pg_changes, sql, rows):
    p = pg_changes.propose('demo', sql, 'operator')
    assert pg_changes.query('demo', 'SELECT * FROM orders ORDER BY id')['rows'] == [(1,10),(2,20)]
    result = approve(pg_changes, p)
    restarted = MutationService(pg_changes.store, pg_changes.policies)
    assert approve(restarted, p) == result
    assert restarted.inspect(p['id'])['status'] == 'completed'
    assert restarted.query('demo', 'SELECT * FROM orders ORDER BY id')['rows'] == rows


def test_live_create_drop(pg_changes):
    approve(pg_changes, pg_changes.propose('demo', 'CREATE TABLE new_table(id INTEGER PRIMARY KEY, name TEXT)', 'operator'))
    approve(pg_changes, pg_changes.propose('demo', "INSERT INTO new_table VALUES (1,'test')", 'operator'))
    p = pg_changes.propose('demo', 'DROP TABLE new_table', 'operator')
    assert p['affected_rows'] == 1
    assert approve(pg_changes, p)['status'] == 'completed'


def test_live_stale_snapshot(pg_changes):
    p = pg_changes.propose('demo', 'DELETE FROM orders WHERE id=1', 'operator')
    approve(pg_changes, pg_changes.propose('demo', 'UPDATE orders SET amount=12 WHERE id=1', 'operator'))
    with pytest.raises(ValueError, match='changed since preview'):
        approve(pg_changes, p)


def test_live_concurrent_retries(pg_changes):
    p = pg_changes.propose('demo', 'UPDATE orders SET amount=amount+1 WHERE id=1', 'operator')
    def attempt(_):
        try:
            return approve(pg_changes, p)
        except ValueError as exc:
            assert 'already executing' in str(exc)
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [r for r in pool.map(attempt, range(2)) if r is not None]
    assert results and all(r == approve(pg_changes, p) for r in results)
    assert pg_changes.query('demo', 'SELECT amount FROM orders WHERE id=1')['rows'] == [(11,)]


@pytest.mark.parametrize('sql', [
    'DELETE FROM orders', 'DROP SCHEMA public CASCADE', 'SELECT pg_sleep(1)',
    'UPDATE orders SET amount=0 WHERE id=1; DROP TABLE orders',
    'INSERT INTO orders VALUES (3,30),(4,40),(5,50)',
    'CREATE TABLE new_table(id SERIAL)', 'CREATE TABLE new_table AS SELECT * FROM orders',
    'DELETE FROM orders WHERE id IN (SELECT id FROM orders)',
])
def test_live_reject_unsupported(pg_changes, sql):
    with pytest.raises(ValueError):
        pg_changes.propose('demo', sql, 'operator')


def test_live_constraint_error_rolls_back(pg_changes):
    import psycopg
    p = pg_changes.propose('demo', 'INSERT INTO orders VALUES (3,30),(1,40)', 'operator')
    with pytest.raises(psycopg.Error):
        approve(pg_changes, p)
    assert pg_changes.query('demo', 'SELECT count(*) FROM orders')['rows'] == [(2,)]


def test_live_query_blocks_write(pg_changes):
    with pytest.raises(ValueError):
        pg_changes.query('demo', 'DELETE FROM orders WHERE id=1')
    with pytest.raises(ValueError):
        pg_changes.query('demo', 'SELECT * FROM pg_catalog.pg_authid')
