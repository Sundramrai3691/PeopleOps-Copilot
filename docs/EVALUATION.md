# PeopleOps Copilot evaluation

`data/peopleops/evaluation.json` contains 33 cases for policy retrieval, workforce aggregates, follow-ups, authorization, ambiguity, approval behavior, and adversarial inputs. Run the deterministic supported-path evaluator with `python -m scripts.evaluation.evaluate_peopleops`.

## Current measurements

On the checked local run, the evaluator exercised 29 of 33 cases: 29 passed and 0 failed. Four cases were not exercised: two ambiguous requests and two unsupported write types (rejecting a seeded request and changing manager assignment). The score covers deterministic policy retrieval, fixed headcount SQL, role rejection, mutation syntax rejection, and one leave proposal. It is not a model accuracy, latency, retry, or benchmark result. The service behaviors also have assertions in `tests/test_peopleops.py`.

## Scoring plan

For model-backed HR evaluation, record the model/version and configuration, data snapshot, number of attempted cases, and raw per-case outcomes. Report:

- policy source and section match rate;
- analytics answer exact match and SQL execution validity;
- correct tool selection;
- unauthorized access rejection rate;
- proportion of sensitive writes held for approval;
- request latency and bounded retry count.

Do not infer these measurements from hand-authored expected cases. Preserve failed and ambiguous cases in the denominator.

## Foundation artifacts

Legacy SQL-agent and BIRD materials in `data/benchmarks/`, `docs/evidence/`, and related scripts describe upstream or historical research. They are not PeopleOps evaluation results and are not included in PeopleOps metrics.
