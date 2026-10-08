"""Start a temporary loopback API/worker deployment and check real HTTP flow.

Uses an existing demo tasks.json. Stops only processes started by this script.
Does not claim public deployment, worker crash recovery, or tenant isolation.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tasks', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--initial-sql', default='', help='Optional candidate; recorded in results, not a blind accuracy test')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    for key in ('SQL_AGENT_DATABASE_URL',):
        env.pop(key, None)
    token = secrets.token_hex(24)
    env.update(SQL_AGENT_SQL_TASKS=str(args.tasks.resolve()),
        SQL_AGENT_JOB_DB=str((args.output / 'jobs.sqlite').resolve()),
        SQL_AGENT_API_KEYS=json.dumps({token: {'name': 'smoke', 'role': 'operator'}}),
        SQL_AGENT_PLANNER_PROVIDER='ollama', SQL_AGENT_PLANNER_BASE_URL='http://127.0.0.1:11434',
        SQL_AGENT_PLANNER_MODEL='qwen3:8b')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'

    def request(path, payload=None, authenticated=True):
        headers = {'content-type': 'application/json', 'idempotency-key': 'smoke-paid-orders'}
        if authenticated:
            headers['x-api-key'] = token
        req = urllib.request.Request(base + path, headers=headers,
            data=None if payload is None else json.dumps(payload).encode())
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    processes = []
    try:
        with (args.output / 'service.log').open('w') as log:
            processes.append(subprocess.Popen([sys.executable, '-m', 'uvicorn',
                'sql_agent.api:create_app', '--factory', '--host', '127.0.0.1',
                '--port', str(port)], env=env, stdout=log, stderr=log))
            deadline = time.monotonic() + 20
            while True:
                try:
                    request('/health')
                    break
                except (OSError, urllib.error.URLError):
                    if time.monotonic() > deadline:
                        raise RuntimeError('API failed to start')
                    time.sleep(.2)
            try:
                request('/v1/tasks', authenticated=False)
                raise AssertionError('Unauthenticated request was accepted')
            except urllib.error.HTTPError as exc:
                assert exc.code in (401, 403)
            payload = {'task_id': 'commerce:paid_orders', 'initial_sql': args.initial_sql}
            first = request('/v1/jobs', payload)
            duplicate = request('/v1/jobs', payload)
            assert first['job']['job_id'] == duplicate['job']['job_id']
            assert duplicate['created'] is False
            config = json.loads(args.tasks.read_text())
            verified = next(t for t in config if t['task_id'] == payload['task_id'])['contract'].get('verification_sql')
            if verified:
                try:
                    request('/v1/jobs', {**payload, 'question': 'Different business question'})
                    raise AssertionError('Verified question override accepted')
                except urllib.error.HTTPError as exc:
                    assert exc.code == 422
            # Submission before worker start checks durable queuing.
            processes.append(subprocess.Popen([sys.executable, '-m', 'sql_agent.worker'],
                env=env, stdout=log, stderr=log))
            deadline = time.monotonic() + 120
            while True:
                job = request('/v1/jobs/' + first['job']['job_id'])
                if job['status'] in ('completed', 'needs_review', 'failed'):
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError('Worker did not finish')
                time.sleep(.2)
            correct = (job['status'] == 'completed' and job['result']['output'] ==
                       {'columns': ['order_id'], 'rows': [[101], [102], [103], [105]]})
            summary = {'scope': 'temporary loopback deployment, no public service',
                'unauthenticated_denied': True, 'duplicate_submission_deduplicated': True,
                'queued_before_worker_start': True, 'real_model_answer_correct': correct,
                'initial_sql': args.initial_sql,
                'verified_question_override_denied': bool(verified),
                'job': job}
            (args.output / 'summary.json').write_text(json.dumps(summary, indent=2))
            assert correct, 'HTTP flow completed but independent business answer check failed; see summary.json'
            print('PASS: real HTTP API → durable queue → worker → Ollama → correct SQL result; authentication and idempotency checked.')
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
