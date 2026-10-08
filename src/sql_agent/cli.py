from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from .jobs import SqliteJobRepository
from .pipeline import JobPipeline
from .worker import Worker


def main():
    parser = argparse.ArgumentParser(description="Run a registered SQL Data Agent task locally")
    parser.add_argument("--task", default="employee_names")
    parser.add_argument("--question", default=None)
    parser.add_argument("--sql", default="", help="Initial query to inspect or repair")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="sql_agent-sql-") as directory:
        repository = SqliteJobRepository(Path(directory) / "jobs.db")
        pipeline = JobPipeline()
        task = pipeline.registry.get(args.task)
        job, _ = repository.submit("sql_analysis", {
            "task_id": args.task, "question": args.question, "initial_sql": args.sql,
            "_business_context_sha256": task.context_sha256,
            "_execution_contract_sha256": task.contract.snapshot().sha256}, "cli")
        worker = Worker(repository, pipeline)
        while worker.run_once():
            pass
        stored = repository.get(job.job_id)
        print(json.dumps(stored.to_dict(), indent=2))
        if stored.status == "failed":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
