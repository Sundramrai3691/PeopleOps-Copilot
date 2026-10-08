"""Small, explicitly allowlisted PostgreSQL CRUD profile.

Preview uses SELECT/EXPLAIN (never EXPLAIN ANALYZE), not a rollback-based dry
run. Only ordinary tables with primitive columns and no executable defaults,
triggers, RLS, inheritance, foreign keys or custom constraints are admitted.
This deliberately narrow profile is not a general database administration tool.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
import time

from sqlglot import exp, parse
from sqlglot.errors import SqlglotError

from .mutations import RECEIPTS, digest
from .sql_validation import READ_QUERY_TYPES


@dataclass(frozen=True)
class PostgresPolicy:
    dsn_env: str
    schema: str
    tables: tuple[str, ...]
    allow_ddl: bool = False
    max_rows: int = 100
    timeout_seconds: float = 5
    max_database_bytes: int = 16 * 1024 * 1024
    approval_seconds: int = 900
    engine: str = "postgresql"

    def __post_init__(self):
        object.__setattr__(self, "tables", tuple(self.tables))
        if not self.schema or self.schema.startswith("pg_") or self.schema == "information_schema":
            raise ValueError("an application schema is required")
        if not self.tables or any(t.lower().startswith(("pg_", "_sql_agent_")) for t in self.tables):
            raise ValueError("explicit non-reserved table allowlist required")
        if not os.environ.get(self.dsn_env):
            raise ValueError("PostgreSQL DSN environment variable is missing")
        if min(self.max_rows, self.timeout_seconds, self.max_database_bytes, self.approval_seconds) <= 0:
            raise ValueError("mutation limits must be positive")

    def fingerprint(self):
        # Bind approval to the configured connection without exposing credentials.
        return digest({**asdict(self), "connection_hash": hashlib.sha256(os.environ.get(self.dsn_env, "").encode()).hexdigest()})


ALLOWED_NODES = {
    "Insert", "Update", "Delete", "Create", "Drop", "Schema", "Table", "Identifier",
    "Column", "Literal", "Null", "Boolean", "EQ", "NEQ", "GT", "GTE", "LT", "LTE",
    "And", "Or", "Not", "Paren", "In", "Between", "Is", "Neg", "Add", "Sub", "Mul",
    "Div", "Mod", "Tuple", "Values", "Where", "Star", "Select", "From", "Order", "Ordered",
    "Limit", "Offset", "Count", "Sum", "Avg", "Min", "Max", "Coalesce", "Lower", "Upper",
    "Length", "Distinct", "Alias", "Union", "Intersect", "Except", "ColumnDef", "DataType", "DataTypeParam",
    "ColumnConstraint", "PrimaryKeyColumnConstraint", "NotNullColumnConstraint", "PrimaryKey",
}
TYPES = {"INT", "BIGINT", "SMALLINT", "TEXT", "VARCHAR", "CHAR", "BOOLEAN", "DECIMAL", "FLOAT", "DOUBLE", "DATE", "TIMESTAMP"}


def statement(sql, policy, read=False):
    try:
        nodes = parse(sql, read="postgres")
    except SqlglotError as exc:
        raise ValueError("invalid PostgreSQL SQL") from exc
    roots = READ_QUERY_TYPES if read else (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop)
    if len(nodes) != 1 or not isinstance(nodes[0], roots):
        raise ValueError("one supported SQL statement required")
    node = nodes[0]
    for item in node.walk():
        if type(item).__name__ not in ALLOWED_NODES:
            raise ValueError(f"unsupported PostgreSQL expression: {type(item).__name__}")
        if isinstance(item, exp.DataType) and item.this.value not in TYPES:
            raise ValueError("only primitive column types are supported")
    if any(node.args.get(k) for k in ("returning", "conflict", "alternative", "cascade", "replace", "exists", "with_", "using", "from_")) and not read:
        raise ValueError("conflict modes, conditional DDL, joined mutations and cascades are unsupported")
    if isinstance(node, (exp.Update, exp.Delete)) and not node.args.get("where"):
        raise ValueError("UPDATE and DELETE require WHERE")
    if isinstance(node, exp.Insert) and not isinstance(node.expression, exp.Values):
        raise ValueError("INSERT currently requires explicit VALUES")
    if isinstance(node, (exp.Create, exp.Drop)):
        if not policy.allow_ddl or str(node.args.get("kind")).upper() != "TABLE":
            raise ValueError("table-management permission required")
    if isinstance(node, exp.Create) and (not isinstance(node.this, exp.Schema) or node.expression):
        raise ValueError("explicit-column CREATE TABLE required")
    for table in node.find_all(exp.Table):
        if table.name not in policy.tables or table.catalog or table.db not in {"", policy.schema}:
            raise ValueError("table/schema not allowlisted")
        table.set("db", exp.to_identifier(policy.schema, quoted=True))
    target = None if read else (node.this.this if isinstance(node.this, exp.Schema) else node.this).name
    return node, node.sql(dialect="postgres", identify=True), target


def connect(policy, readonly=False):
    import psycopg
    conn = psycopg.connect(os.environ[policy.dsn_env], connect_timeout=max(1, int(policy.timeout_seconds)))
    # Explicit transaction, with no caller-controlled search path or SQL settings.
    conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY" if readonly else "BEGIN")
    conn.execute("SELECT pg_catalog.set_config('search_path', 'pg_catalog', true)")
    for setting in ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout"):
        conn.execute("SELECT pg_catalog.set_config(%s, %s, true)", (setting, str(int(policy.timeout_seconds * 1000))))
    return conn


def relation(policy, table):
    from psycopg import sql
    return sql.Identifier(policy.schema, table)


def inventory(conn, policy, lock=False, *, read_only=False):
    from psycopg import sql
    result = []
    for table in sorted(policy.tables):
        row = conn.execute("""SELECT c.oid, c.relkind, c.relrowsecurity, c.relhassubclass, c.relispartition
            FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND c.relname=%s""", (policy.schema, table)).fetchone()
        if not row:
            result.append((table, None, []))
            continue
        oid, kind, rls, children, partition = row
        if kind != 'r' or (rls and not read_only) or children or partition:
            raise ValueError("only ordinary non-RLS, non-inherited tables are supported")
        if conn.execute('SELECT 1 FROM pg_catalog.pg_inherits WHERE inhrelid=%s', (oid,)).fetchone():
            raise ValueError('inherited tables are unsupported')
        if lock:
            conn.execute(sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(relation(policy, table)))
            current = conn.execute('SELECT pg_catalog.to_regclass(%s)::oid',
                                   (relation(policy, table).as_string(conn),)).fetchone()[0]
            if current != oid:
                raise ValueError('table identity changed while acquiring lock')
        if conn.execute("SELECT 1 FROM pg_catalog.pg_trigger WHERE tgrelid=%s AND NOT tgisinternal", (oid,)).fetchone():
            raise ValueError("tables with user triggers are unsupported")
        if conn.execute("SELECT 1 FROM pg_catalog.pg_rewrite WHERE ev_class=%s", (oid,)).fetchone():
            raise ValueError("tables with rules are unsupported")
        if conn.execute("SELECT 1 FROM pg_catalog.pg_constraint WHERE (conrelid=%s OR confrelid=%s) AND contype NOT IN ('p','u','n')", (oid, oid)).fetchone():
            raise ValueError("foreign keys and custom constraints are unsupported")
        columns = conn.execute("""SELECT a.attname, t.typname, a.atttypmod, a.attnotnull, a.attidentity, a.attgenerated,
                n.nspname, a.atthasdef
            FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
            JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace
            WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""", (oid,)).fetchall()
        builtin = {'int2','int4','int8','text','varchar','bpchar','bool','numeric','float4','float8','date','timestamp'}
        if any(c[1] not in builtin or c[4] or c[5] or c[6] != 'pg_catalog' or c[7] for c in columns):
            raise ValueError("only primitive columns without defaults/identity/generated expressions are supported")
        indexes = conn.execute("SELECT pg_catalog.pg_get_indexdef(indexrelid) FROM pg_catalog.pg_index WHERE indrelid=%s ORDER BY indexrelid", (oid,)).fetchall()
        if conn.execute('SELECT 1 FROM pg_catalog.pg_index WHERE indrelid=%s AND (indexprs IS NOT NULL OR indpred IS NOT NULL)', (oid,)).fetchone():
            raise ValueError('expression and partial indexes are unsupported')
        result.append((table, oid, [columns, indexes]))
    return result


def snapshot(conn, policy, lock=False):
    from psycopg import sql
    metadata = inventory(conn, policy, lock=lock)
    result = hashlib.sha256(json.dumps(metadata, default=str).encode())
    total = 0
    counts = {}
    until = time.monotonic() + policy.timeout_seconds
    for table, oid, _ in metadata:
        if oid is None:
            continue
        hashes = []
        # Server-side cursor avoids fetching an unbounded table into Python.
        with conn.cursor(name="snapshot_rows") as cursor:
            cursor.execute(sql.SQL("SELECT xmin::text, ctid::text, * FROM {}").format(relation(policy, table)))
            for row in cursor:
                raw = json.dumps(row, default=str).encode()
                total += len(raw)
                if total > policy.max_database_bytes or len(hashes) >= 100000 or time.monotonic() > until:
                    raise ValueError("database exceeds bounded preview budget")
                hashes.append(hashlib.sha256(raw).digest())
        counts[table] = len(hashes)
        for value in sorted(hashes):
            result.update(value)
    return result.hexdigest(), counts


def preview(policy, sql):
    from psycopg import sql as psql
    node, executable, target = statement(sql, policy)
    with connect(policy, readonly=True) as conn:
        fingerprint, counts = snapshot(conn, policy)
        if isinstance(node, exp.Create):
            if target in counts:
                raise ValueError("table already exists")
            affected = 0
        elif isinstance(node, exp.Drop):
            if target not in counts:
                raise ValueError("table does not exist")
            affected = counts[target]
        else:
            # EXPLAIN without ANALYZE does not execute the mutation.
            conn.execute("EXPLAIN (FORMAT JSON) " + executable).fetchone()
            if isinstance(node, exp.Insert):
                affected = len(node.expression.expressions)
            else:
                condition = node.args["where"].sql(dialect="postgres", identify=True)
                affected = conn.execute(psql.SQL("SELECT count(*) FROM {} ").format(relation(policy, target)) + psql.SQL(condition)).fetchone()[0]
        if affected > policy.max_rows:
            raise ValueError("mutation exceeds row limit")
    return type(node).__name__.upper(), target, fingerprint, affected


def receipt(conn, policy, ident):
    from psycopg import sql
    exists = conn.execute("SELECT 1 FROM pg_catalog.pg_tables WHERE schemaname=%s AND tablename=%s", (policy.schema, RECEIPTS)).fetchone()
    if not exists:
        return None
    row = conn.execute(sql.SQL("SELECT result FROM {} WHERE id=%s").format(relation(policy, RECEIPTS)), (ident,)).fetchone()
    return json.loads(row[0]) if row else None


def execute(policy, body, reviewer):
    from psycopg import sql
    node, executable, target = statement(body["sql"], policy)
    with connect(policy) as conn:
        # Serialize receipts/DDL for this schema; table locks also exclude
        # ordinary writers while rechecking and committing the preview.
        lock_key = int.from_bytes(hashlib.sha256(policy.schema.encode()).digest()[:8], 'big', signed=True)
        conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (lock_key,))
        previous = receipt(conn, policy, body["id"])
        if previous:
            return previous
        if time.time() > body["expires_at"]:
            raise ValueError("approval expired; propose again")
        fingerprint, counts = snapshot(conn, policy, lock=True)
        if fingerprint != body["snapshot_sha256"]:
            raise ValueError("database changed since preview; propose again")
        cursor = conn.execute(executable)
        affected = counts[target] if isinstance(node, exp.Drop) else (0 if isinstance(node, exp.Create) else cursor.rowcount)
        if affected != body["affected_rows"] or affected > policy.max_rows:
            raise ValueError("execution impact differs from approved preview; rolled back")
        result = dict(id=body["id"], status="completed", affected_rows=affected,
                      proposal_sha256=body["proposal_sha256"], reviewer=reviewer, completed_at=time.time())
        conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} (id TEXT PRIMARY KEY, result TEXT NOT NULL)").format(relation(policy, RECEIPTS)))
        conn.execute(sql.SQL("INSERT INTO {} VALUES (%s,%s)").format(relation(policy, RECEIPTS)), (body["id"], json.dumps(result)))
        return result  # context manager commits effect + receipt together


def query(policy, sql):
    _, executable, _ = statement(sql, policy, read=True)
    with connect(policy, readonly=True) as conn:
        inventory(conn, policy, read_only=True)
        with conn.cursor(name="read_rows") as cursor:
            cursor.execute(executable)
            rows = cursor.fetchmany(policy.max_rows + 1)
            return {"columns": [c.name for c in cursor.description], "rows": rows[:policy.max_rows],
                    "truncated": len(rows) > policy.max_rows}
