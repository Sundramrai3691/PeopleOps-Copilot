"""Local three-container credential boundary acceptance; SQLite, explicit SQL only."""
import argparse
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import time
import urllib.request
import uuid


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True,exist_ok=False)
    args.output.chmod(0o700)
    for name in ('control','business'):
        (args.output/name).mkdir()
    with sqlite3.connect(args.output/'business/business.db') as conn:
        conn.execute('CREATE TABLE orders(id INTEGER PRIMARY KEY, amount INTEGER)')
        conn.execute('INSERT INTO orders VALUES (1,10)')
    config={'control_store':'/control/changes.db','databases':{'business':{
        'database':'/business/business.db','tables':['orders'],'allow_ddl':False}}}
    (args.output/'mutations.json').write_text(json.dumps(config))
    gateway_token=secrets.token_hex(24)
    admin=secrets.token_hex(24)
    env={'SQL_AGENT_MUTATION_CONFIG':'/config/mutations.json','SQL_AGENT_JOB_DB':'/control/jobs.db'}
    mounts=[str(args.output/'control')+':/control',str(args.output/'mutations.json')+':/config/mutations.json:ro']
    worker={'image':args.image,'environment':env,'volumes':mounts+[str(args.output/'business')+':/business:ro'],
            'command':['sql-agent-worker','--poll-seconds','0.1'],'healthcheck':{'disable':True}}
    api={'image':args.image,'volumes':worker['volumes'],'ports':['127.0.0.1:0:8000'],
         'environment':{**env,'SQL_AGENT_GATEWAY_URL':'http://gateway:8001','SQL_AGENT_GATEWAY_TOKEN':gateway_token,
             'SQL_AGENT_API_KEYS':json.dumps({admin:{'name':'human','role':'admin'}})}}
    gateway={'image':args.image,'volumes':mounts+[str(args.output/'business')+':/business'],
        'environment':{**env,'SQL_AGENT_GATEWAY_TOKEN':gateway_token},
        'healthcheck':{'test':['CMD','python','-c',"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health')"],
                       'interval':'2s','timeout':'2s','retries':20},
        'command':['uvicorn','sql_agent.gateway:create_app','--factory','--host','0.0.0.0','--port','8001']}
    compose=args.output/'compose.json'
    compose.write_text(json.dumps({'services':{'api':api,'worker':worker,'gateway':gateway}}))
    compose.chmod(0o600)
    project='sql-agent-gateway-'+uuid.uuid4().hex[:10]
    command=['docker','compose','-p',project,'-f',str(compose)]
    evidence={'project':project,'scope':'single-host trusted control store, SQLite, explicit SQL; no LLM load or multi-tenant claim'}
    logs=[]
    def run(*parts,check=True):
        result=subprocess.run(command+list(parts),capture_output=True,text=True,timeout=90)
        logs.append((result.stdout+result.stderr).replace(admin,'<REDACTED>').replace(gateway_token,'<REDACTED>'))
        if check and result.returncode:
            raise RuntimeError('container operation failed; inspect redacted logs')
        return result
    def call(path,payload=None):
        req=urllib.request.Request(base+path,data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Content-Type':'application/json','x-api-key':admin,'idempotency-key':'gateway-acceptance'})
        with urllib.request.urlopen(req,timeout=10) as response:
            return json.load(response)
    try:
        run('up','-d','--wait','--wait-timeout','60','api','gateway')
        run('up','-d','worker')
        base='http://'+run('port','api','8000').stdout.strip()
        result=run('exec','-T','worker','python','-c',
            "import sqlite3; sqlite3.connect('/business/business.db').execute('UPDATE orders SET amount=999')",check=False)
        assert result.returncode != 0 and 'readonly' in result.stderr.lower()
        evidence['worker_direct_write_denied']=True
        result=run('exec','-T','worker','python','-c',
                   "import os; assert not os.getenv('SQL_AGENT_GATEWAY_TOKEN'); assert not os.getenv('SQL_AGENT_API_KEYS')")
        evidence['worker_has_no_gateway_or_admin_token']=result.returncode==0
        created=call('/v1/requests',{'database_id':'business','sql':'UPDATE orders SET amount=amount+1 WHERE id=1'})
        ident=created['job']['job_id']
        deadline=time.monotonic()+30
        while True:
            job=call('/v1/requests/'+ident)
            if job['status']=='needs_review': break
            if job['status']=='failed' or time.monotonic()>deadline: raise RuntimeError('proposal did not reach review')
            time.sleep(.1)
        with sqlite3.connect(args.output/'business/business.db') as conn:
            assert conn.execute('SELECT amount FROM orders').fetchone()[0]==10
        approval={'decision':'approve','proposal_sha256':job['result']['proposal']['proposal_sha256']}
        first=call('/v1/requests/'+ident+'/review',approval)
        second=call('/v1/requests/'+ident+'/review',approval)
        assert first['mutation_result']==second['mutation_result']
        with sqlite3.connect(args.output/'business/business.db') as conn:
            assert conn.execute('SELECT amount FROM orders').fetchone()[0]==11
        evidence.update(approved_gateway_write_once=True,receipt=first['mutation_result'],passed=True)
        print(json.dumps(evidence),flush=True)
    finally:
        run('logs','--no-color','--tail','60',check=False)
        cleanup=run('down',check=False)
        evidence['containers_removed']=cleanup.returncode==0
        (args.output/'evidence.json').write_text(json.dumps(evidence,indent=2))
        (args.output/'container.log').write_text('\n'.join(logs))


if __name__=='__main__': main()
