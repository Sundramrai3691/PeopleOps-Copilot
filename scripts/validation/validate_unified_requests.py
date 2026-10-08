"""Two live-model smoke cases, using only a disposable SQLite database."""
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sql_agent.api import create_app
from sql_agent.jobs import SqliteJobRepository
from sql_agent.mutations import MutationPolicy, MutationService
from sql_agent.pipeline import JobPipeline
from sql_agent.planner import OllamaPlannerModel
from sql_agent.security import ApiKeyAuthorizer, Principal
from sql_agent.sql_config import SQLTaskRegistry, DemoSQLPlanner
from sql_agent.worker import Worker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='qwen3:14b')
    parser.add_argument('--base-url', default='http://127.0.0.1:11434')
    parser.add_argument('--expect-rag', action='store_true', help='Require retrieved passages in each result')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='sql-agent-unified-smoke-') as directory:
        root = Path(directory)
        db = root / 'business.sqlite'
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE orders(id INTEGER PRIMARY KEY, amount INTEGER)')
            conn.execute('INSERT INTO orders VALUES(1,10)')
        service = MutationService(root / 'control.sqlite', {'demo': MutationPolicy(db, ('orders',))})
        jobs = SqliteJobRepository(root / 'jobs.sqlite')
        registry = SQLTaskRegistry()
        keys = ApiKeyAuthorizer({'operator': Principal('smoke','operator'), 'admin': Principal('smoke-admin','admin')})
        pipeline = JobPipeline(registry, DemoSQLPlanner(), mutations=service)
        pipeline.planner = SimpleNamespace(model=OllamaPlannerModel(args.base_url, args.model, timeout=60, max_tokens=512))
        worker = Worker(jobs, pipeline)
        with TestClient(create_app(jobs, registry, keys, service)) as client:
            evidence = []
            for key, question in [('read', 'Show the amount of order 1.'), ('change', 'Set the amount of order 1 to 25.')]:
                response = client.post('/v1/requests', json={'database_id':'demo','question':question},
                                       headers={'x-api-key':'operator','idempotency-key':key})
                response.raise_for_status()
                ident = response.json()['job']['job_id']
                worker.run_once()
                job = jobs.get(ident)
                if args.expect_rag:
                    assert job.result.get('retrieval', {}).get('hits'), job.to_dict()
                if key == 'read':
                    assert job.status == 'needs_review', job.to_dict()
                    assert job.result['output'] is None, job.result
                    assert job.result['candidate_output']['rows'] == [[10]], job.result
                else:
                    assert job.status == 'needs_review', job.to_dict()
                    assert service.query('demo','SELECT amount FROM orders WHERE id=1')['rows'] == [(10,)]
                    response = client.post('/v1/requests/'+ident+'/review', headers={'x-api-key':'admin'},
                        json={'decision':'approve','proposal_sha256':job.result['proposal']['proposal_sha256']})
                    response.raise_for_status()
                    assert service.query('demo','SELECT amount FROM orders WHERE id=1')['rows'] == [(25,)]
                evidence.append({'case':key, 'sql':job.result['sql'], 'status_before_review':job.status,
                                 'retrieved_ids':[h['id'] for h in job.result.get('retrieval',{}).get('hits',[])]})
            print(json.dumps({'model':args.model,'cases':evidence,'scope':'two synthetic live-model smoke cases, not an accuracy benchmark'}, indent=2))


if __name__ == '__main__':
    main()
