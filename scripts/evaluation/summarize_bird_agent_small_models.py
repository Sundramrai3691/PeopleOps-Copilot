"""Summarize the local SQL-Agent model comparison on BIRD Mini-Dev."""
import json
import sys
from pathlib import Path


def main():
    root = Path(sys.argv[1])
    rows = []
    for name in ('qwen3-4b', 'gemma3-4b', 'qwen3-0.6b'):
        summary = json.loads((root/name/'summary.json').read_text())
        rows.append(dict(model=name, completed=summary['completed'], correct=summary['correct'],
                         accuracy=summary['accuracy_500'], tokens_per_correct=summary['tokens_per_correct'],
                         p95_seconds=summary['p95_task_seconds']))
    report = dict(scope='SQL-Agent: full schema, no RAG; fixed qwen2.5-coder:7b reviewer; generic Planner prompt',
                  results=rows)
    (root/'comparison.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__': main()
