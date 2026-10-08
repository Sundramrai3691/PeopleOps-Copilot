import json
import hashlib
from pathlib import Path

from sql_agent.bounded_runtime import ActionProposal, BoundedAgentRuntime
from sql_agent.sql_environment import SQLiteRepairEnvironment, case_database, expected_output


ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "data/benchmarks/sql_repair_v3_confirmatory.json"
REGISTRATION = ROOT / "data/benchmarks/sql_repair_v3_preregistration.json"
GENERATIONS = ROOT / "data/benchmarks/sql_repair_v3_qwen3_8b_generations.json"


def hydrated_cases():
    payload = json.loads(SUITE.read_text())
    for raw in payload["cases"]:
        case = dict(raw)
        case.update(payload["databases"][case["domain"]])
        yield case


def test_v3_is_balanced_held_out_and_multi_schema():
    payload = json.loads(SUITE.read_text())
    cases = list(hydrated_cases())
    assert payload["confirmatory"] is True
    assert len(cases) == 108
    assert len({c["domain"] for c in cases}) == 6
    assert len({c["cluster_id"] for c in cases}) == 18
    assert {a: sum(c["expected_action"] == a for c in cases) for a in ("KEEP", "REPAIR", "STOP")} == {"KEEP": 36, "REPAIR": 36, "STOP": 36}


def test_v3_gold_queries_execute_and_verify():
    checked = 0
    for case in hydrated_cases():
        if case["expected_action"] == "STOP":
            continue
        columns, rows = expected_output(case)
        proposal = ActionProposal(case["expected_action"], "sql_query", {"sql": case["gold_sql"]}, "oracle")
        outcome = BoundedAgentRuntime().run(proposal, SQLiteRepairEnvironment(case_database(case), columns, rows))
        assert outcome.decision == "KEEP", case["id"]
        checked += 1
    assert checked == 72


def test_v3_domains_and_templates_do_not_overlap_v2():
    v2 = json.loads((ROOT / "data/benchmarks/sql_repair_v2.json").read_text())
    v3 = json.loads(SUITE.read_text())
    assert set(v2["databases"]).isdisjoint(v3["databases"])
    assert {c["failure_family"] for c in v2["cases"]}.isdisjoint({c["failure_family"] for c in v3["cases"]})


def test_v3_registration_and_frozen_generations_bind_exact_suite():
    suite_hash = hashlib.sha256(SUITE.read_bytes()).hexdigest()
    registration = json.loads(REGISTRATION.read_text())
    generations = json.loads(GENERATIONS.read_text())
    suite = json.loads(SUITE.read_text())
    assert registration["case_suite_sha256"] == suite_hash
    assert generations["case_suite_sha256"] == suite_hash
    assert set(generations["generations"]) == {case["id"] for case in suite["cases"]}
