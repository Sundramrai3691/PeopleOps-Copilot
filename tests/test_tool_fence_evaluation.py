"""Pytest integration for ToolFence AI Agent Security Evaluation Benchmark.

Validates the complete 24-case security benchmark covering:
- Category A: Direct instruction overrides & SQL injection
- Category B: Indirect prompt injection & RAG manipulation
- Category C: Tool authorization & privilege escalation
- Category D: Multi-turn state isolation & session revocation
- Category E: Benign controls & legitimate employee workflows
"""
import json
from pathlib import Path
import pytest

from scripts.evaluation.run_tool_fence import BenchmarkContext, run_single_case, ROOT

DATASET_PATH = ROOT / "evals/agent_security_cases.jsonl"
CASES = [json.loads(line) for line in DATASET_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(scope="module")
def benchmark_ctx(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("toolfence-pytest-")
    return BenchmarkContext(Path(tmp))


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_toolfence_benchmark_case(case, benchmark_ctx):
    outcome = run_single_case(case, benchmark_ctx)
    assert outcome["passed"] is True, f"Failed {case['id']} ({case['threat_category']}): {outcome['evidence']}"

