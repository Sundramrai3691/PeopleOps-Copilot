from __future__ import annotations

import re
import sqlite3
from typing import Any

from .bounded_runtime import ActionProposal


MUTATING = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace)\b", re.I)


class SQLiteRepairEnvironment:
    """Disposable SQL environment with a read-only policy and deterministic verifier."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        expected_columns: tuple[str, ...] = (),
        expected_rows: tuple[tuple[Any, ...], ...] | None = None,
    ) -> None:
        self.connection = connection
        self.expected_columns = expected_columns
        self.expected_rows = expected_rows

    def authorize(self, proposal: ActionProposal) -> tuple[bool, str]:
        if proposal.action.upper() not in {"KEEP", "REPAIR"}:
            return False, "unregistered_action"
        if proposal.tool != "sql_query":
            return False, "unregistered_tool"
        sql = str((proposal.arguments or {}).get("sql", "")).strip()
        if not sql or MUTATING.search(sql) or not sql.lower().startswith("select"):
            return False, "read_only_policy_violation"
        if ";" in sql.rstrip(";"):
            return False, "multiple_statements_forbidden"
        return True, "authorized_read_only_query"

    def execute(self, proposal: ActionProposal) -> dict[str, Any]:
        sql = str((proposal.arguments or {})["sql"])
        cursor = self.connection.execute(sql)
        return {"columns": tuple(item[0] for item in cursor.description), "rows": tuple(cursor.fetchall())}

    def verify(self, proposal: ActionProposal, output: Any) -> tuple[bool, str]:
        if self.expected_columns and tuple(output["columns"]) != self.expected_columns:
            return False, "output_contract_mismatch"
        if self.expected_rows is not None and tuple(output["rows"]) != self.expected_rows:
            return False, "output_value_mismatch"
        return True, "execution_and_contract_verified"


def demo_database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE employees(id INTEGER PRIMARY KEY, name TEXT, department TEXT, salary INTEGER);
        INSERT INTO employees(name, department, salary) VALUES
          ('Ada', 'AI', 150000), ('Grace', 'Systems', 140000), ('Linus', 'Systems', 130000);
    """)
    return connection


def case_database(case: dict[str, Any]) -> sqlite3.Connection:
    """Build a fresh in-memory database for a benchmark case.

    V1 cases omit ``setup_sql`` and retain the original employee fixture. V2
    cases carry their own hidden setup so the same evaluator can cover multiple
    schemas without exposing seed rows to the planner prompt.
    """
    setup_sql = str(case.get("setup_sql", "")).strip()
    if not setup_sql:
        return demo_database()
    connection = sqlite3.connect(":memory:")
    connection.executescript(setup_sql)
    return connection


def expected_output(case: dict[str, Any]) -> tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]] | None:
    """Execute the hidden gold query on a fresh case database."""
    gold_sql = str(case.get("gold_sql", "")).strip()
    if not gold_sql:
        return None
    cursor = case_database(case).execute(gold_sql)
    columns = tuple(item[0] for item in cursor.description)
    rows = tuple(cursor.fetchall())
    return columns, rows
