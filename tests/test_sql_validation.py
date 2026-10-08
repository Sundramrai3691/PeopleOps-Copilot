import pytest

from sql_agent.postgres_mutations import PostgresPolicy, statement
from sql_agent.sql_validation import parse_read_query


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 UNION SELECT 2",
        "SELECT 1 INTERSECT SELECT 1",
        "SELECT 1 EXCEPT SELECT 2",
    ],
)
def test_compound_queries_have_one_shared_read_classification(sql, monkeypatch):
    assert parse_read_query(sql, "sqlite") is not None
    monkeypatch.setenv("TEST_POSTGRES_DSN", "postgresql://unused")
    policy = PostgresPolicy("TEST_POSTGRES_DSN", "public", ("items",))
    node, executable, target = statement(sql, policy, read=True)
    assert node is not None
    assert executable
    assert target is None


def test_mutation_is_not_a_read_query():
    with pytest.raises(ValueError, match="read-only"):
        parse_read_query("DELETE FROM items", "sqlite")
