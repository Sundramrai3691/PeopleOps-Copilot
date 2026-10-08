"""Validate the published BIRD comparison summary without private run artifacts."""

import json
from pathlib import Path


EXPECTED = {
    "sql_agent": {
        "qwen3:4b": (196, 0.392),
        "gemma3:4b": (108, 0.216),
        "qwen3:0.6b": (37, 0.074),
    },
    "pv_sql": {
        "qwen3:4b": (180, 0.36),
        "gemma3:4b": (110, 0.22),
        "qwen3:0.6b": (34, 0.068),
    },
}


def main() -> None:
    evidence = (
        Path(__file__).resolve().parents[2]
        / "docs/evidence/2026-09-13/model_agent_comparison.json"
    )
    report = json.loads(evidence.read_text())
    if report["tasks"] != 500:
        raise ValueError("Unexpected BIRD task denominator")

    for workflow, expected_models in EXPECTED.items():
        actual_models = report[workflow]["results"]
        if set(actual_models) != set(expected_models):
            raise ValueError(f"Unexpected models for {workflow}")
        for model, (correct, accuracy) in expected_models.items():
            actual = actual_models[model]
            if actual["correct"] != correct or actual["accuracy"] != accuracy:
                raise ValueError(f"Published metric differs: {workflow}/{model}")
            if abs(correct / report["tasks"] - accuracy) > 1e-12:
                raise ValueError(f"Invalid denominator: {workflow}/{model}")
            if actual["tokens_per_correct"] <= 0 or actual["p95_seconds"] <= 0:
                raise ValueError(f"Invalid cost or latency: {workflow}/{model}")

    print("Verified: BIRD denominator, execution accuracy, token cost, and latency.")


if __name__ == "__main__":
    main()
