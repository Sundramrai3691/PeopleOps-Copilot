"""Opt-in SQLite changes: sandbox preview, explicit approval, atomic receipts.

This is separate from the read-only analysis/retry pipeline. Never give an
autonomous planner an administrator key. Only small, trusted local databases
are supported; database files and this control store are operator-owned.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


RECEIPTS = "_sql_agent_mutation_receipts"
SQLITE_READ_FUNCTIONS = frozenset({'count','sum','avg','min','max','coalesce',
    'lower','upper','length','substr','substring','strftime',
    'like','nullif','abs','group_concat','date','current_timestamp','iif','instr'})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class MutationPolicy:
    database: Path
    tables: tuple[str, ...]
    allow_ddl: bool = False
    max_rows: int = 100
    timeout_seconds: float = 5
    max_database_bytes: int = 16 * 1024 * 1024
    approval_seconds: int = 900
    engine: str = "sqlite"

    def __post_init__(self):
        object.__setattr__(self, "database", Path(self.database).resolve())
        object.__setattr__(self, "tables", tuple(self.tables))
        if not self.database.is_file() or not self.tables:
            raise ValueError("mutation database must exist and tables must be allowlisted")
        if any(t.lower().startswith(("sqlite_", "_sql_agent_")) for t in self.tables):
            raise ValueError("reserved table name")
        if min(self.max_rows, self.timeout_seconds, self.max_database_bytes, self.approval_seconds) <= 0:
            raise ValueError("mutation limits must be positive")

    def fingerprint(self):
        values = asdict(self)
        values["database"] = str(self.database)
        return digest(values)


def validate_sql(sql, policy):
    from sqlglot import exp, parse
    from sqlglot.errors import ParseError

    if not isinstance(sql, str) or not 0 < len(sql) <= 20000:
        raise ValueError("SQL must contain 1–20000 characters")
    try:
        statements = parse(sql, read="sqlite")
    except ParseError as exc:
        raise ValueError("invalid SQLite SQL") from exc
    if len(statements) != 1 or not isinstance(statements[0], (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop)):
        raise ValueError("exactly one INSERT, UPDATE, DELETE, CREATE TABLE or DROP TABLE required")
    node = statements[0]
    ddl = isinstance(node, (exp.Create, exp.Drop))
    if ddl and (not policy.allow_ddl or str(node.args.get("kind")).upper() != "TABLE"):
        raise ValueError("DDL requires explicit table-management permission")
    if isinstance(node, (exp.Update, exp.Delete)) and not node.args.get("where"):
        raise ValueError("UPDATE and DELETE require WHERE; row limits still apply")
    if node.args.get("returning") or node.args.get("conflict") or node.args.get("alternative"):
        raise ValueError("RETURNING and conflict/replace modes are not supported")
    if isinstance(node, exp.Create) and (not isinstance(node.this, exp.Schema) or node.args.get("expression")):
        raise ValueError("only explicit-column CREATE TABLE is supported")
    target = node.this.this if isinstance(node.this, exp.Schema) else node.this
    if not isinstance(target, exp.Table) or target.name not in policy.tables:
        raise ValueError("target table not allowlisted")
    for table in node.find_all(exp.Table):
        if table.db or table.catalog or table.name not in policy.tables:
            raise ValueError("only unqualified allowlisted tables are supported")
    return type(node).__name__.upper(), target.name


def _connect(policy, readonly=False):
    # mode=rw never creates a missing/replaced database by accident.
    conn = sqlite3.connect(policy.database.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                           uri=True, timeout=policy.timeout_seconds, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA trusted_schema=OFF")
    return conn


def _deadline(conn, policy):
    until = time.monotonic() + policy.timeout_seconds
    conn.set_progress_handler(lambda: int(time.monotonic() > until), 1000)


def _fingerprint(conn, policy):
    # Logical snapshot also covers triggers, indexes, constraints and receipts.
    size = conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0]
    if size > policy.max_database_bytes:
        raise ValueError("database exceeds small-database mutation limit")
    result = hashlib.sha256()
    total = 0
    for line in conn.iterdump():
        data = (line + "\n").encode()
        total += len(data)
        if total > policy.max_database_bytes * 4:
            raise ValueError("snapshot exceeds mutation limit")
        result.update(data)
    return result.hexdigest()


def _execute_guarded(conn, sql, operation, target, policy):
    # Enforce at SQLite's execution boundary, not just the parser. Do not permit
    # triggers, views, attached databases, extension functions or other tables.
    ddl = operation in {"CREATE", "DROP"}
    allowed_reads = set(policy.tables) | ({"sqlite_master", "sqlite_schema"} if ddl else set())
    def authorize(action, first, second, database, source):
        if source is not None or database not in {None, "main"}:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and first in allowed_reads:
            return sqlite3.SQLITE_OK
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            if first == target or (ddl and first in {"sqlite_master", "sqlite_schema"}):
                return sqlite3.SQLITE_OK
        if ddl and action in {sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE} and first == target:
            return sqlite3.SQLITE_OK
        if ddl and action in {sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_DROP_INDEX} and second == target:
            return sqlite3.SQLITE_OK
        # No SQL functions in this first write profile (including random/time).
        return sqlite3.SQLITE_DENY

    _deadline(conn, policy)
    drop_rows = 0
    if operation == "DROP":
        quoted = '"' + target.replace('"', '""') + '"'
        drop_rows = conn.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
        if drop_rows > policy.max_rows:
            raise ValueError("DROP exceeds row limit")
    before = conn.total_changes
    conn.set_authorizer(authorize)
    try:
        conn.execute(sql)
        affected = drop_rows if operation == "DROP" else conn.total_changes - before
        if affected > policy.max_rows:
            raise ValueError("mutation exceeds row limit; transaction rolled back")
        return affected
    finally:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)


class MutationService:
    def __init__(self, store, policies):
        self.store = Path(store).resolve()
        self.policies = dict(policies)
        protected = {self.store, Path(str(self.store) + '.graph.sqlite')}
        if any(getattr(p, 'database', None) in protected for p in self.policies.values()):
            raise ValueError("mutation control store must be separate from business data")
        self.store.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.store)) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, body TEXT NOT NULL, reviewer TEXT, decision TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS execution_attempts (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
            conn.commit()
        from .database_workflow import DatabaseWorkflow
        self.workflow = DatabaseWorkflow(self)

    @classmethod
    def from_env(cls):
        config = os.environ.get("SQL_AGENT_MUTATION_CONFIG")
        if not config:
            return None
        path = Path(config).resolve()
        values = json.loads(path.read_text())
        policies = {}
        for key, value in values["databases"].items():
            if value.get('engine', 'sqlite') == 'sqlite':
                policies[key] = MutationPolicy(database=path.parent / value["database"],
                    **{k: v for k, v in value.items() if k != "database"})
            elif value['engine'] == 'postgresql':
                from .postgres_mutations import PostgresPolicy
                policies[key] = PostgresPolicy(**value)
            else:
                raise ValueError('unsupported database engine')
        return cls(path.parent / values["control_store"], policies)

    def propose(self, database_id, sql, submitted_by):
        return self.workflow.start(database_id, sql, submitted_by)

    def query(self, database_id, sql):
        return self.workflow.start(database_id, sql, mode='query')

    def review(self, ident, proposal_sha256, reviewer, decision):
        return self.workflow.resume(ident, proposal_sha256, reviewer, decision)

    def _propose(self, database_id, sql, submitted_by, ident=None):
        if ident:
            with closing(sqlite3.connect(self.store)) as conn:
                row = conn.execute('SELECT body FROM proposals WHERE id=?', (ident,)).fetchone()
            if row:
                body = json.loads(row[0])
                if (body['database_id'], body['sql'], body['submitted_by']) != (database_id, sql, submitted_by):
                    raise ValueError('proposal id reused for different inputs')
                return {**body, 'status': 'needs_approval'}
        policy = self.policies.get(database_id)
        if policy is None:
            raise ValueError("database not enabled for mutations")
        if policy.engine == 'postgresql':
            from .postgres_mutations import preview
            operation, target, snapshot, affected = preview(policy, sql)
        else:
            operation, target = validate_sql(sql, policy)
            with closing(_connect(policy, readonly=True)) as source, closing(sqlite3.connect(":memory:", isolation_level=None)) as sandbox:
                source.execute("BEGIN")
                _deadline(source, policy)
                snapshot = _fingerprint(source, policy)
                source.backup(sandbox)
                source.rollback()
                sandbox.execute("PRAGMA foreign_keys=ON")
                sandbox.execute("PRAGMA trusted_schema=OFF")
                sandbox.execute("BEGIN")
                affected = _execute_guarded(sandbox, sql, operation, target, policy)
                sandbox.rollback()
        body = dict(id=ident or str(uuid.uuid4()), database_id=database_id, sql=sql, operation=operation,
                    target=target, affected_rows=affected, snapshot_sha256=snapshot,
                    policy_sha256=policy.fingerprint(), submitted_by=submitted_by,
                    expires_at=time.time() + policy.approval_seconds)
        body["proposal_sha256"] = digest(body)
        with closing(sqlite3.connect(self.store)) as conn:
            conn.execute("INSERT INTO proposals(id,body) VALUES (?,?)", (body["id"], json.dumps(body)))
            conn.commit()
        return {**body, "status": "needs_approval"}

    def get(self, ident):
        with closing(sqlite3.connect(self.store)) as conn:
            row = conn.execute("SELECT body,reviewer,decision FROM proposals WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise ValueError("unknown mutation proposal")
        return json.loads(row[0]), row[1], row[2]

    def describe(self):
        return [dict(database_id=key, engine=p.engine, tables=list(p.tables),
                     allow_ddl=p.allow_ddl, max_rows=p.max_rows) for key, p in self.policies.items()]

    def inspect(self, ident):
        body, reviewer, decision = self.get(ident)
        status = "needs_approval" if not decision else ("rejected" if decision == "reject" else "approved_pending_execution")
        with closing(sqlite3.connect(self.store)) as control:
            attempt = control.execute('SELECT status FROM execution_attempts WHERE id=?', (ident,)).fetchone()
        if attempt:
            status = 'execution_failed' if attempt[0] == 'failed' else 'execution_unknown'
        policy = self.policies.get(body["database_id"])
        if policy and decision == "approve":
            if policy.engine == 'postgresql':
                from .postgres_mutations import connect, receipt
                with connect(policy, readonly=True) as conn:
                    saved = receipt(conn, policy, ident)
                    if saved:
                        return {**body, **saved}
            else:
                with closing(_connect(policy, readonly=True)) as conn:
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (RECEIPTS,)).fetchone():
                        receipt = conn.execute(f"SELECT result FROM {RECEIPTS} WHERE id=?", (ident,)).fetchone()
                        if receipt:
                            return {**body, **json.loads(receipt[0])}
        return {**body, "reviewer": reviewer, "status": status}

    def _query(self, database_id, sql):
        from .sql_validation import parse_read_query
        policy = self.policies.get(database_id)
        if policy is None:
            raise ValueError("unknown database")
        if policy.engine == 'postgresql':
            from .postgres_mutations import query
            return query(policy, sql)
        parse_read_query(sql, "sqlite")
        with closing(_connect(policy, readonly=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            _deadline(conn, policy)
            def authorize(action, first, second, database, source):
                if source is not None or database not in {None, "main"}:
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_SELECT or (action == sqlite3.SQLITE_READ and first in policy.tables):
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION and second in SQLITE_READ_FUNCTIONS:
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY
            conn.set_authorizer(authorize)
            cursor = conn.execute(sql)
            rows = cursor.fetchmany(policy.max_rows + 1)
            return {"columns": [c[0] for c in cursor.description], "rows": rows[:policy.max_rows],
                    "truncated": len(rows) > policy.max_rows}

    def _review(self, ident, proposal_sha256, reviewer, decision, *, local_execution=False):
        if decision not in {"approve", "reject"}:
            raise ValueError("invalid decision")
        # Persist authorization before attempting the business transaction.
        with closing(sqlite3.connect(self.store)) as control:
            control.execute("BEGIN IMMEDIATE")
            row = control.execute("SELECT body,reviewer,decision FROM proposals WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise ValueError("unknown mutation proposal")
            body = json.loads(row[0])
            if proposal_sha256 != body["proposal_sha256"]:
                raise ValueError("approval must match the exact preview hash")
            if digest({k: v for k, v in body.items() if k != "proposal_sha256"}) != proposal_sha256:
                raise ValueError("stored proposal integrity failure")
            if row[2] and (row[2] != decision or row[1] != reviewer):
                raise ValueError("proposal already reviewed")
            control.execute("UPDATE proposals SET reviewer=?,decision=? WHERE id=?", (reviewer, decision, ident))
            control.commit()
        if decision == "reject":
            return {"id": ident, "status": "rejected", "reviewer": reviewer}
        policy = self.policies.get(body["database_id"])
        if policy is None or policy.fingerprint() != body["policy_sha256"]:
            raise ValueError("mutation policy changed; propose again")
        gateway_url = os.getenv('SQL_AGENT_GATEWAY_URL', '').strip()
        if gateway_url and not local_execution:
            from .gateway import execute_remote
            return execute_remote(gateway_url, os.getenv('SQL_AGENT_GATEWAY_TOKEN',''), ident, proposal_sha256)
        # Persist intent before touching the business DB. A crash leaves an
        # uncertain attempt, not permission to re-run a mutation without proof.
        with closing(sqlite3.connect(self.store)) as control:
            control.execute('BEGIN IMMEDIATE')
            previous = control.execute('SELECT status FROM execution_attempts WHERE id=?', (ident,)).fetchone()
            if previous is None:
                control.execute('INSERT INTO execution_attempts VALUES (?,?)', (ident, 'pending'))
            control.commit()
        if previous is not None:
            observed = self.inspect(ident)
            if observed['status'] == 'completed':
                return {k:observed[k] for k in ('id','status','affected_rows','proposal_sha256','reviewer','completed_at')}
            raise ValueError('execution unknown or terminal; reconcile authoritative receipt before any retry')
        try:
            result = self._execute_approved(policy, body, reviewer)
        except ValueError:
            with closing(sqlite3.connect(self.store)) as control:
                control.execute('UPDATE execution_attempts SET status=? WHERE id=?', ('failed', ident))
                control.commit()
            raise
        except Exception as exc:
            if isinstance(exc, sqlite3.IntegrityError) or str(getattr(exc, 'sqlstate', '')).startswith('23'):
                # Explicit constraint rejection propagated after transaction
                # rollback is a known failure, unlike a lost commit response.
                with closing(sqlite3.connect(self.store)) as control:
                    control.execute('UPDATE execution_attempts SET status=? WHERE id=?', ('failed', ident))
                    control.commit()
                raise
            # Includes connection loss before/after COMMIT. No receipt observed
            # later is not proof of rollback; keep pending/UNKNOWN indefinitely.
            raise ValueError('execution unknown; inspect authoritative receipt, do not replay') from exc
        with closing(sqlite3.connect(self.store)) as control:
            control.execute('UPDATE execution_attempts SET status=? WHERE id=?', ('succeeded', ident))
            control.commit()
        return result

    def _execute_approved(self, policy, body, reviewer):
        ident = body['id']
        proposal_sha256 = body['proposal_sha256']
        if policy.engine == 'postgresql':
            from .postgres_mutations import execute
            return execute(policy, body, reviewer)
        operation, target = validate_sql(body["sql"], policy)
        with closing(_connect(policy)) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (RECEIPTS,)).fetchone():
                    receipt = conn.execute(f"SELECT result FROM {RECEIPTS} WHERE id=?", (ident,)).fetchone()
                    if receipt:
                        conn.rollback()
                        return json.loads(receipt[0])
                if time.time() > body["expires_at"]:
                    raise ValueError("approval expired; propose again")
                _deadline(conn, policy)
                if _fingerprint(conn, policy) != body["snapshot_sha256"]:
                    raise ValueError("database changed since preview; propose again")
                affected = _execute_guarded(conn, body["sql"], operation, target, policy)
                if affected != body["affected_rows"]:
                    raise ValueError("execution impact differs from approved preview")
                result = dict(id=ident, status="completed", affected_rows=affected,
                              proposal_sha256=proposal_sha256, reviewer=reviewer, completed_at=time.time())
                # Receipt and mutation commit together: retrying this proposal
                # after a lost HTTP response cannot repeat its business effect.
                conn.execute(f"CREATE TABLE IF NOT EXISTS {RECEIPTS} (id TEXT PRIMARY KEY, result TEXT NOT NULL)")
                conn.execute(f"INSERT INTO {RECEIPTS} VALUES (?,?)", (ident, json.dumps(result)))
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise
