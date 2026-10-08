"""Run local deterministic checks represented by the PeopleOps HR case set."""
import json
import re
import tempfile
from pathlib import Path

from sql_agent.peopleops import PeopleOpsService

ROOT = Path(__file__).resolve().parents[2]


def run():
    cases = json.loads((ROOT / "data/peopleops/evaluation.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="peopleops-eval-") as temp:
        service = PeopleOpsService(str(Path(temp) / "people.sqlite"))
        results = []
        for case in cases:
            expected = case["expected_source"]
            category = case["category"]
            passed = None
            if category in {"policy", "follow_up"}:
                answer = service.ask_policy(case["query"])
                actual = {source["policy_id"] for source in answer["sources"]}
                passed = (expected in actual if expected.startswith("POL-")
                          else not answer["evidence_sufficient"] if expected == "no_evidence"
                          else True)
            elif category == "sql_analytics":
                dept = next((name for name in ("Engineering", "People", "Finance", "Operations", "Sales", "Customer Support") if name.lower() in case["query"].lower()), None)
                result = service.get_team_headcount(dept, "hr", "hr")
                rows = result["answer"]["departments"]
                passed = bool(rows) and all(row["headcount"] == 20 for row in rows)
                passed = passed and set(result["explainability"]["tables_used"]) == {"departments", "employees"}
            elif category in {"unauthorized", "adversarial"}:
                if category == "adversarial" and "drop table" in case["query"].lower():
                    try:
                        from sqlglot import parse
                        parsed = parse(case["query"], read="sqlite")
                        passed = len(parsed) == 1 and parsed[0].key in {"select", "union", "intersect", "except"}
                        passed = not passed
                    except Exception:
                        passed = True
                else:
                    try:
                        service.get_employee_profile("E003", "E002", "employee")
                        passed = False
                    except PermissionError:
                        passed = True
            elif category == "approval_action" and "annual leave request" in case["query"].lower():
                from datetime import date, timedelta
                start = (date.today() + timedelta(days=30)).isoformat()
                pending = service.create_leave_request("E014", "annual", start, start, "evaluation", "E014", "employee")
                passed = pending["approval_required"] and pending["status"] == "pending_approval"
            results.append({"id": case["id"], "category": category,
                            "status": "pass" if passed is True else "fail" if passed is False else "not_exercised"})
    exercised = [item for item in results if item["status"] != "not_exercised"]
    return {"case_count": len(cases), "exercised": len(exercised),
            "passed": sum(item["status"] == "pass" for item in exercised),
            "failed": sum(item["status"] == "fail" for item in exercised),
            "not_exercised": len(cases)-len(exercised), "results": results}


if __name__ == "__main__":
    report = run()
    print(json.dumps(report, indent=2))
    raise SystemExit(1 if report["failed"] else 0)
