"""Start/stop a loopback-only SQL-Agent demo using the current Python interpreter."""
import argparse
from dataclasses import asdict, replace
from datetime import datetime,timezone,timedelta
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

from sql_agent.commerce_catalog import make_task
from sql_agent.business_context import MetricDefinition
from sql_agent.data_agent import DataContract
from sql_agent.sql_config import SQLTask

ROOT=Path(__file__).resolve().parents[2]
RUNTIME=ROOT/'runtime/local-demo'
PORT=8765


def alive(pid):
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False


def prepare():
    RUNTIME.mkdir(parents=True,exist_ok=True)
    tasks=[]
    for variant in ('healthy','stale','invalid'):
        path=RUNTIME/(variant+'.sqlite')
        # Only refresh the disposable demo source files after the previous stack stopped.
        if path.exists():path.unlink()
        db=sqlite3.connect(path)
        db.executescript('CREATE TABLE orders(id INTEGER PRIMARY KEY, amount_cents INTEGER); CREATE TABLE refunds(order_id INTEGER, amount_cents INTEGER, status TEXT); CREATE TABLE ingestion_log(completed_at TEXT); INSERT INTO orders VALUES(1,10000),(2,8000); INSERT INTO refunds VALUES(1,1000,"approved"),(1,2000,"pending");')
        ts=datetime.now(timezone.utc)-timedelta(days=3 if variant=='stale' else 0)
        db.execute('INSERT INTO ingestion_log VALUES(?)',(ts.isoformat(),))
        if variant=='invalid':db.execute('UPDATE orders SET amount_cents=NULL WHERE id=1')
        db.commit();db.close()
        if variant=='healthy':
            tasks.extend(make_task(metric,path) for metric in ('net_revenue','gross_revenue','approved_refunds'))
            tasks.append(make_task('net_revenue',path,task_id='free_analysis_review',verified=False))
        else:tasks.append(make_task('net_revenue',path,task_id='blocked_'+variant))
    # One general-query demonstration, separate from the verified metric catalog.
    # Reuses the documented synthetic billing fixture, never its offline oracle.
    from scripts.evaluation.transfer_sql_cases import CASES, fixture, create_database
    billing_path=RUNTIME/'billing.sqlite'
    if billing_path.exists():billing_path.unlink()
    create_database(billing_path,fixture(0))
    billing_task=next(c for c in CASES if c.case_id=='invoice_balance').task(billing_path)
    tasks.append(replace(billing_task,definitions=(MetricDefinition(
        'settled_receipt',('settled','receipt','receipts'),
        "A settled receipt is a receipts row whose state is exactly 'settled'. Pending receipts are not settled. "
        "Settlement is determined by state, including rows whose amount is NULL. "
        "Receipts link to invoices through receipts.invoice_id = invoices.id.",
        'synthetic billing dictionary','1'),)))
    from scripts.evaluation.commerce_acceptance_cases import fixture as analysis_fixture, create_database as analysis_database, DEMO_QUESTIONS
    analysis_path=RUNTIME/'analytics.sqlite'
    if analysis_path.exists():analysis_path.unlink()
    analysis_database(analysis_path,analysis_fixture(17))
    tasks.append(SQLTask('commerce_analysis',DEMO_QUESTIONS[0],DataContract((),dynamic_columns=True,max_rows=200),analysis_path,example_questions=DEMO_QUESTIONS))
    config=[]
    for t in tasks:
        config.append({'task_id':t.task_id,'question':t.question,'contract':asdict(t.contract),
            'database':t.database.name,'definitions':[asdict(d) for d in t.definitions],
            'quality_checks':[asdict(c) for c in t.quality_checks], 'example_questions':list(t.example_questions)})
    (RUNTIME/'tasks.json').write_text(json.dumps(config,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','stop','status'])
    parser.add_argument('--model',default='qwen3:8b')
    parser.add_argument('--generation-format',choices=['json','sql'],default='json')
    parser.add_argument('--max-tokens',type=int,help='Override the bounded output token budget (1-8192)')
    parser.add_argument('--thinking',action='store_true',help='Qwen3 SQL reasoning profile: 8192 output tokens, 120s per request')
    args=parser.parse_args()
    if args.thinking and args.generation_format != 'sql':parser.error('--thinking requires --generation-format sql')
    max_tokens=args.max_tokens if args.max_tokens is not None else (8192 if args.thinking else 2048 if args.generation_format=='sql' else 256)
    if not 1 <= max_tokens <= 8192:parser.error('--max-tokens must be between 1 and 8192')
    state_path=RUNTIME/'processes.json';state=json.loads(state_path.read_text()) if state_path.exists() else {}
    if args.action=='status':
        print(json.dumps({**state,'alive':{k:alive(v) for k,v in state.get('pids',{}).items()}},indent=2));return
    if args.action=='stop':
        for name,pid in state.get('pids',{}).items():
            # Check ownership marker to avoid terminating a reused/unrelated PID.
            cmd=Path(f'/proc/{pid}/cmdline')
            if cmd.exists() and b'sql_agent' in cmd.read_bytes():os.kill(pid,signal.SIGTERM)
        for _ in range(30):
            if all(not Path(f'/proc/{pid}').exists() or 'Z' in Path(f'/proc/{pid}/stat').read_text().split()[2] for pid in state.get('pids',{}).values()):break
            time.sleep(.1)
        state_path.unlink(missing_ok=True);print('Stopped local demo. Source and job history retained.');return
    if any(alive(pid) for pid in state.get('pids',{}).values()):raise SystemExit('Existing demo processes are alive; use status or stop first.')
    with socket.socket() as sock:
        # Match the server's restart behavior; closed TCP connections in
        # TIME_WAIT do not mean another service is listening on this port.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:sock.bind(('127.0.0.1',PORT))
        except OSError:raise SystemExit(f'Port {PORT} is occupied; no existing process was changed.')
    with urllib.request.urlopen('http://127.0.0.1:11434/api/tags',timeout=5) as response:
        if not any(m['name']==args.model for m in json.load(response)['models']):raise SystemExit('Requested model is not installed in Ollama.')
    prepare()
    env=os.environ.copy()
    for key in ('SQL_AGENT_DATABASE_URL',):env.pop(key,None)
    env.update(PYTHONPATH=str(ROOT/'src'),SQL_AGENT_JOB_DB=str(RUNTIME/'jobs.sqlite'),
        SQL_AGENT_SQL_TASKS=str(RUNTIME/'tasks.json'),SQL_AGENT_PLANNER_PROVIDER='ollama',
        SQL_AGENT_PLANNER_BASE_URL='http://127.0.0.1:11434',SQL_AGENT_PLANNER_MODEL=args.model,
        SQL_AGENT_SQL_GENERATION_FORMAT=args.generation_format,
        SQL_AGENT_PLANNER_THINKING='true' if args.thinking else 'false',
        SQL_AGENT_PLANNER_TIMEOUT_SECONDS='120' if args.thinking else '30',
        SQL_AGENT_PLANNER_MAX_TOKENS=str(max_tokens),
        SQL_AGENT_LOCAL_DEMO='1',SQL_AGENT_API_KEYS=json.dumps({'123':{'name':'local-presenter','role':'admin'}}))
    procs={}
    commands={'api':[sys.executable,'-m','uvicorn','sql_agent.api:create_app','--factory','--host','127.0.0.1','--port',str(PORT)],
              'worker':[sys.executable,'-m','sql_agent.worker']}
    try:
        for name,cmd in commands.items():
            with (RUNTIME/(name+'.log')).open('ab') as log:
                procs[name]=subprocess.Popen(cmd,cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        state={'url':f'http://127.0.0.1:{PORT}','pids':{k:p.pid for k,p in procs.items()},'started_at':datetime.now(timezone.utc).isoformat(),'demo_key':'123','model':args.model,'generation_format':args.generation_format,'thinking':args.thinking,'max_tokens':max_tokens}
        state_path.write_text(json.dumps(state,indent=2))
        for _ in range(40):
            if any(p.poll() is not None for p in procs.values()):raise RuntimeError('A service exited; inspect runtime/local-demo/*.log')
            try:
                with urllib.request.urlopen(state['url']+'/health',timeout=1) as r:
                    if r.status==200:print(json.dumps(state,indent=2));return
            except OSError:time.sleep(.25)
        raise RuntimeError('API did not become healthy')
    except Exception:
        for p in procs.values():p.terminate()
        state_path.unlink(missing_ok=True)
        raise

if __name__=='__main__':main()
