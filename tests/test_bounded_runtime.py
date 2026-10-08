from sql_agent.bounded_runtime import ActionProposal, BoundedAgentRuntime
from sql_agent.sql_environment import (
    SQLiteRepairEnvironment,
    case_database,
    demo_database,
    expected_output,
)


def test_sql_environment_keeps_verified_read_only_query():
    env = SQLiteRepairEnvironment(demo_database(), ("name",))
    proposal = ActionProposal("REPAIR", "sql_query", {
        "sql": "SELECT name FROM employees WHERE department = 'Systems' ORDER BY name"
    })
    outcome = BoundedAgentRuntime().run(proposal, env)
    assert outcome.decision == "KEEP"
    assert outcome.output["rows"] == (("Grace",), ("Linus",))


def test_sql_environment_stops_mutating_or_unregistered_actions():
    env = SQLiteRepairEnvironment(demo_database())
    for proposal in (
        ActionProposal("REPAIR", "sql_query", {"sql": "DROP TABLE employees"}),
        ActionProposal("REPAIR", "shell", {"sql": "SELECT * FROM employees"}),
    ):
        outcome = BoundedAgentRuntime().run(proposal, env)
        assert outcome.decision == "STOP"
        assert not any(step["step"] == "execute" for step in outcome.trajectory)


def test_sql_environment_stops_tool_error_and_contract_mismatch():
    env = SQLiteRepairEnvironment(demo_database(), ("name",))
    invalid = ActionProposal("REPAIR", "sql_query", {"sql": "SELECT missing FROM employees"})
    assert BoundedAgentRuntime().run(invalid, env).decision == "STOP"
    mismatch = ActionProposal("REPAIR", "sql_query", {"sql": "SELECT salary FROM employees"})
    assert BoundedAgentRuntime().run(mismatch, env).reason == "output_contract_mismatch"


def test_case_database_and_value_contract_support_multiple_schemas():
    case = {
        "setup_sql": "CREATE TABLE products(id INTEGER, sku TEXT); INSERT INTO products VALUES(1, 'A');",
        "gold_sql": "SELECT sku FROM products ORDER BY id",
    }
    columns, rows = expected_output(case)
    env = SQLiteRepairEnvironment(case_database(case), columns, rows)
    correct = ActionProposal("REPAIR", "sql_query", {"sql": "SELECT sku FROM products ORDER BY id"})
    wrong = ActionProposal("REPAIR", "sql_query", {"sql": "SELECT sku FROM products WHERE id=2"})
    assert BoundedAgentRuntime().run(correct, env).decision == "KEEP"
    assert BoundedAgentRuntime().run(wrong, env).reason == "output_value_mismatch"
