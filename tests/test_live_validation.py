import json
from pathlib import Path

from sql_agent.bounded_runtime import ActionProposal, BoundedAgentRuntime, RuntimeOutcome
from sql_agent.data_agent import SQLAgentSession, DataContract
from scripts.evaluation.validate_live_sql_agent import database, grade


def test_generation_failure_is_not_counted_as_successful_refusal():
    case = {"expected": "STOP"}
    error = RuntimeOutcome("STOP", "planner_error:TimeoutError", None, ())
    refusal = RuntimeOutcome("STOP", "planner_stop", None, ())
    assert not grade(case, error)["success"]
    assert grade(case, refusal)["success"]


def test_transfer_reference_queries_satisfy_predeclared_contracts():
    cases = json.loads((Path(__file__).parents[1] / "data/benchmarks/sql_contract_transfer_v1.json").read_text())
    for case in cases:
        if case["expected"] == "STOP":
            continue
        db = database(case)
        try:
            result = BoundedAgentRuntime().run(ActionProposal("REPAIR", "sql_query", {"sql": case["gold_sql"]}),
                SQLAgentSession(db, DataContract(**case["contract"])))
            assert result.decision == "KEEP", case["id"]
            assert grade(case, result)["success"], case["id"]
        finally:
            db.close()
