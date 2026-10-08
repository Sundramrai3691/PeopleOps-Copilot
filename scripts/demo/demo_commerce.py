"""One-command real-model business demo through the durable job/worker path."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from sql_agent.data_agent import SQLAgentPlanner, DataContract
from sql_agent.jobs import SqliteJobRepository
from sql_agent.pipeline import JobPipeline
from sql_agent.planner import OllamaPlannerModel
from sql_agent.sql_config import SQLTask, SQLTaskRegistry
from sql_agent.worker import Worker
from scripts.evaluation.validate_live_sql_agent import RecordingModel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('runtime/commerce-demo'))
    parser.add_argument('--base-url', default='http://127.0.0.1:11434')
    parser.add_argument('--model', default='qwen3:8b')
    parser.add_argument('--all', action='store_true', help='Run all cases, including known semantic failures')
    parser.add_argument('--verified', action='store_true', help='Use registered business-query checks; not blind SQL evaluation')
    parser.add_argument('--catalog-fallback', action='store_true', help='Explicitly permit registered query fallback; requires --verified')
    args = parser.parse_args()
    if args.catalog_fallback and not args.verified:
        parser.error('--catalog-fallback requires --verified')
    root = Path(__file__).resolve().parents[2]
    suite = json.loads((root / 'data/benchmarks/commerce_v1.json').read_text())
    # Refuse to overwrite previous evidence or database state.
    args.output.mkdir(parents=True, exist_ok=False)
    db_path = (args.output / 'commerce.sqlite').resolve()
    db = sqlite3.connect(db_path)
    db.executescript((root / 'data/demo/commerce.sql').read_text())
    db.close()
    tasks = [dict(task_id=c['id'], question=c['goal'], database=db_path.name,
                  contract=c['contract']) for c in suite]
    if args.verified:
        tasks = json.loads((root / 'data/demo/commerce_tasks.json').read_text())
        if args.catalog_fallback:
            for task in tasks:
                if task['contract'].get('verification_sql'):
                    task['contract']['fallback_to_verified_query'] = True
    (args.output / 'tasks.json').write_text(json.dumps(tasks, indent=2))
    registry = SQLTaskRegistry(tuple(SQLTask(t['task_id'], t['question'],
        DataContract(**t['contract']), db_path) for t in tasks))
    recorder = RecordingModel(OllamaPlannerModel(args.base_url, args.model, timeout=120))
    repository = SqliteJobRepository(args.output / 'jobs.sqlite')
    worker = Worker(repository, JobPipeline(registry, SQLAgentPlanner(recorder)))
    failed = False
    selected = {'commerce:net_revenue', 'commerce:repair_column', 'commerce:deny_write'}
    for case in suite:
        if not args.all and case['id'] not in selected:
            continue
        start = len(recorder.calls)
        job, _ = repository.submit('sql_analysis', {'task_id': case['id'],
            'initial_sql': case['initial_sql']}, case['id'])
        while worker.run_once():
            pass
        stored = repository.get(job.job_id)
        result = stored.result or {}
        output = result.get('output')
        if case['expected'] == 'KEEP':
            passed = (stored.status == 'completed' and output == {
                'columns': case['contract']['columns'], 'rows': case['expected_rows']})
        else:
            passed = (stored.status == 'needs_review' and result.get('routing', {}).get('reason')
                      in {'planner_stop', 'read_only_policy_violation'})
        failed |= not passed
        artifact = {'job': asdict(stored), 'model_calls': recorder.calls[start:],
                    'offline_expected_outcome_met': passed,
                    'mode': 'registered_business_verification' if args.verified else 'blind_generation'}
        (args.output / (case['id'].replace(':', '_') + '.json')).write_text(json.dumps(artifact, indent=2))
        print(json.dumps({'task': case['id'], 'status': stored.status, 'output': output,
                          'offline_check': passed, 'routing': result.get('routing')}, indent=2), flush=True)
    print(f'Database, API task configuration, jobs and raw model traces: {args.output}')
    raise SystemExit(1 if failed else 0)


if __name__ == '__main__':
    main()
