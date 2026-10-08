"""Paired exploratory/verified evaluation on an imported real-data task catalog.

Registered SQL fallback is never reported as independent model accuracy.
Expected outputs must have been derived independently at import time.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import statistics
import time
import urllib.request

from sql_agent.data_agent import SQLAgentPlanner, DataContract
from sql_agent.jobs import SqliteJobRepository
from sql_agent.pipeline import JobPipeline
from sql_agent.planner import OllamaPlannerModel
from sql_agent.sql_config import SQLTask, SQLTaskRegistry
from sql_agent.worker import Worker
from scripts.evaluation.validate_live_sql_agent import RecordingModel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20:
        parser.error('repeats must be 1..20')
    tasks = json.loads((args.data / 'tasks.json').read_text())
    expected = json.loads((args.data / 'expected.json').read_text())
    source_manifest = json.loads((args.data / 'manifest.json').read_text())
    database_hashes = {t['database']: hashlib.sha256((args.data / t['database']).read_bytes()).hexdigest() for t in tasks}
    if 'sqlite_sha256' in source_manifest and set(database_hashes.values()) != {source_manifest['sqlite_sha256']}:
        raise ValueError('source database differs from frozen import manifest')
    args.output.mkdir(parents=True, exist_ok=False)
    with urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=10) as response:
        model_info = [m for m in json.load(response)['models'] if m['name'] == 'qwen3:8b']
    manifest = {'source': source_manifest, 'database_hashes': database_hashes,
        'agent_source_sha256': hashlib.sha256((Path(__file__).resolve().parents[2] / 'src/sql_agent/data_agent.py').read_bytes()).hexdigest(),
        'model': model_info, 'prompt_version': SQLAgentPlanner.PROMPT_VERSION,
        'tasks_sha256': hashlib.sha256((args.data / 'tasks.json').read_bytes()).hexdigest(),
        'repeats': args.repeats, 'unique_tasks': len(tasks),
        'protocol': 'Same first live response in exploratory and verified arms; production worker path; no online tuning.',
        'scope': 'Project-authored questions on real public data. Repeated tasks are not independent samples. Catalog fallback is a deterministic business query, not model success.'}
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    records = []
    repo = SqliteJobRepository(args.output / 'jobs.sqlite')
    for repeat in range(args.repeats):
        for task in tasks:
            first = None
            for mode in ('exploratory', 'verified_catalog'):
                config = dict(task['contract'])
                if mode == 'exploratory':
                    config.pop('verification_sql', None)
                    config.pop('fallback_to_verified_query', None)
                model = RecordingModel(OllamaPlannerModel('http://127.0.0.1:11434', 'qwen3:8b', timeout=30))
                class Paired:
                    count = 0
                    def complete(self, prompt):
                        self.count += 1
                        if self.count == 1 and first is not None:
                            model.calls.append({'prompt': prompt, 'content': first, 'replayed': True})
                            return first
                        return model.complete(prompt)
                registry = SQLTaskRegistry((SQLTask(task['task_id'], task['question'],
                    DataContract(**config), (args.data / task['database']).resolve()),))
                job, _ = repo.submit('sql_analysis', {'task_id': task['task_id']}, f'{repeat}:{task["task_id"]}:{mode}')
                started = time.perf_counter()
                Worker(repo, JobPipeline(registry, SQLAgentPlanner(Paired()))).run_once()
                elapsed = (time.perf_counter()-started)*1000
                stored = repo.get(job.job_id)
                if mode == 'exploratory':
                    first = model.calls[0].get('content') if model.calls else None
                    if first is None:
                        raise RuntimeError('No initial response; paired experiment invalid')
                result = stored.result or {}
                correct = stored.status == 'completed' and result.get('output') == expected[task['task_id']]
                row = {'task_id': task['task_id'], 'repeat': repeat, 'mode': mode,
                    'correct': correct, 'false_accept': stored.status == 'completed' and not correct,
                    'wall_ms': elapsed, 'job': asdict(stored), 'model_calls': model.calls}
                records.append(row)
                filename = f'{repeat}_{task["task_id"].replace(":", "_")}_{mode}.json'
                (args.output / filename).write_text(json.dumps(row, indent=2))
                print(json.dumps({'task': task['task_id'], 'repeat': repeat, 'mode': mode,
                    'correct': correct, 'status': stored.status, 'reason': result.get('routing', {}).get('reason')}), flush=True)
    summary = {}
    for mode in ('exploratory', 'verified_catalog'):
        selected = [r for r in records if r['mode'] == mode]
        values = sorted(r['wall_ms'] for r in selected)
        summary[mode] = {'runs': len(selected), 'unique_tasks': len(tasks),
            'correct': sum(r['correct'] for r in selected),
            'false_accept': sum(r['false_accept'] for r in selected),
            'catalog_fallback': sum(r['job']['result']['routing']['reason'] == 'registered_business_fallback_verified' for r in selected),
            'median_wall_ms': statistics.median(values), 'p95_wall_ms': values[math.ceil(.95*len(values))-1],
            'live_calls': sum(not c.get('replayed', False) for r in selected for c in r['model_calls'])}
    summary['latency_note'] = 'Verified first response is replayed: timings exclude its inference cost and are not comparable end-to-end latency.'
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
