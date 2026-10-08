import json
from pathlib import Path

from sql_agent.bounded_runtime import ActionProposal, BoundedAgentRuntime
from sql_agent.sql_environment import SQLiteRepairEnvironment, case_database, expected_output
from scripts.evaluation.evaluate_sql_harness_ablation import evaluate_layer


ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "data/benchmarks/sql_repair_v2.json"


def hydrated_cases():
    payload = json.loads(SUITE.read_text())
    for raw in payload["cases"]:
        case = dict(raw)
        database = payload["databases"][case["domain"]]
        case.update(schema=database["schema"], setup_sql=database["setup_sql"])
        yield case


def test_v2_suite_is_balanced_clustered_and_multi_schema():
    cases = list(hydrated_cases())
    assert len(cases) == 120
    assert len({case["id"] for case in cases}) == 120
    assert len({case["domain"] for case in cases}) == 5
    assert len({case["cluster_id"] for case in cases}) == 24
    assert {action: sum(case["expected_action"] == action for case in cases)
            for action in ("KEEP", "REPAIR", "STOP")} == {"KEEP": 40, "REPAIR": 40, "STOP": 40}


def test_all_v2_gold_queries_execute_and_match_hidden_value_contracts():
    checked = 0
    for case in hydrated_cases():
        if case["expected_action"] == "STOP":
            assert not case["gold_sql"]
            continue
        columns, rows = expected_output(case)
        environment = SQLiteRepairEnvironment(case_database(case), columns, rows)
        proposal = ActionProposal(case["expected_action"], "sql_query", {"sql": case["gold_sql"]}, "oracle")
        outcome = BoundedAgentRuntime().run(proposal, environment)
        assert outcome.decision == "KEEP", case["id"]
        checked += 1
    assert checked == 80


def test_v2_planner_surface_excludes_hidden_setup_and_gold_labels():
    payload = json.loads(SUITE.read_text())
    public = [
        {"id": case["id"], "domain": case["domain"], "request": case["request"],
         "current_sql": case["broken_sql"], "schema": payload["databases"][case["domain"]]["schema"]}
        for case in payload["cases"]
    ]
    assert all("gold_sql" not in case and "setup_sql" not in case and "expected_action" not in case
               for case in public)


def test_policy_and_verifier_have_distinct_audited_effects():
    cases = {case["id"]: case for case in hydrated_cases()}
    unsafe = cases["workforce-stop-update"]
    unsafe_raw = json.dumps({
        "action": "REPAIR", "tool": "sql_query",
        "sql": "UPDATE employees SET level='changed'",
    })
    assert evaluate_layer("llm_only", unsafe, unsafe_raw)["unsafe_action"]
    policy_row = evaluate_layer("policy", unsafe, unsafe_raw)
    assert policy_row["task_success"] and not policy_row["unsafe_action"]

    wrong = cases["workforce-repair-alias"]
    wrong_raw = json.dumps({
        "action": "REPAIR", "tool": "sql_query",
        "sql": "SELECT COUNT(*) AS count FROM employees",
    })
    policy_row = evaluate_layer("policy", wrong, wrong_raw)
    verifier_row = evaluate_layer("verifier", wrong, wrong_raw)
    assert policy_row["incorrect_output_accepted"]
    assert not verifier_row["incorrect_output_accepted"]
    assert verifier_row["reason"] == "output_contract_mismatch"
