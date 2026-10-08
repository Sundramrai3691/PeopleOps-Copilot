"""Isolated loopback deployment smoke; scripted registered task, not live-LLM eval.

Requires a prebuilt image and Docker Compose >=2.24.4 (!override). Stops only
its generated project on exit; preserves its database volume and evidence.
"""
import argparse,json,os,secrets,subprocess,time,urllib.request,urllib.error,uuid
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--load-jobs',type=int,default=0)
    args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=False)
    if not 0 <= args.load_jobs <= 1000:
        raise ValueError('load jobs must be between 0 and 1000')
    args.output.chmod(0o700)
    root=Path(__file__).resolve().parents[2]
    project='sql-agent-trial-'+uuid.uuid4().hex[:10]
    password=secrets.token_hex(24); keys={role:secrets.token_hex(24) for role in ('viewer','operator','admin')}
    env=os.environ.copy()
    env.update(SQL_AGENT_DB_PASSWORD=password,SQL_AGENT_API_KEYS=json.dumps({key:{'name':role,'role':role} for role,key in keys.items()}),
               SQL_AGENT_PLANNER_BASE_URL='',SQL_AGENT_PLANNER_MODEL='',SQL_AGENT_PLANNER_PROVIDER='ollama',SQL_AGENT_PLANNER_API_KEY='')
    override=args.output/'compose.override.json'
    # YAML override resets the default host port rather than merging it.
    override=override.with_suffix('.yaml')
    override.write_text('services:\n  api:\n    image: '+json.dumps(args.image)+'\n    ports: !override ["127.0.0.1:0:8000"]\n  worker:\n    image: '+json.dumps(args.image)+'\n')
    command=['docker','compose','-p',project,'-f',str(root/'compose.yaml'),'-f',str(override.resolve())]
    logs=[];evidence={'project':project,'image':args.image,'checks':{},'scope':'Registered scripted task; local container smoke, not LLM quality or production SLO.'}
    def clean(text):
        for secret in (password,*keys.values()):text=text.replace(secret,'<REDACTED>')
        return text
    def compose(*parts):
        result=subprocess.run(command+list(parts),env=env,capture_output=True,text=True,timeout=120)
        logs.append(clean(result.stdout+result.stderr))
        if result.returncode:raise RuntimeError('Compose failed; see redacted compose.log')
        return result.stdout
    def call(path,payload=None,role='operator',idem=None):
        headers={'Content-Type':'application/json'}
        if role:headers['x-api-key']=keys[role]
        if idem:headers['idempotency-key']=idem
        req=urllib.request.Request(base+path,data=json.dumps(payload).encode() if payload is not None else None,headers=headers)
        try:
            with urllib.request.urlopen(req,timeout=5) as response:
                raw=response.read().decode()
                return response.status,json.loads(raw) if 'application/json' in response.headers.get('content-type','') else raw
        except urllib.error.HTTPError as exc:return exc.code,exc.read().decode()
    def wait_health():
        end=time.monotonic()+45
        while time.monotonic()<end:
            try:
                if call('/health',role=None)[0]==200:return
            except OSError:pass
            time.sleep(.2)
        raise TimeoutError('health not ready')
    def wait_job(jid):
        end=time.monotonic()+45
        while time.monotonic()<end:
            code,job=call('/v1/jobs/'+jid,role='viewer')
            if code==200 and job['status'] not in ('queued','running'):return job
            time.sleep(.2)
        raise TimeoutError('job not finished')
    started=False
    try:
        started=True;compose('up','-d','--no-build','--wait','--wait-timeout','90','postgres','api')
        compose('up','-d','--no-build','worker')
        address=compose('port','api','8000').strip(); assert address.startswith('127.0.0.1:')
        base='http://'+address; evidence['observed_url']=base;wait_health()
        evidence['checks']['loopback_health']=True
        assert call('/v1/tasks',role=None)[0]==401
        assert call('/v1/jobs',{'task_id':'employee_names'},role='viewer',idem='denied')[0]==403
        evidence['checks']['authentication_and_role_denial']=True
        compose('stop','worker')
        payload={'task_id':'employee_names','initial_sql':'SELECT missing FROM employees'}
        code,submitted=call('/v1/jobs',payload,idem='registered-repair');assert code==202
        jid=submitted['job']['job_id']
        code,duplicate=call('/v1/jobs',payload,idem='registered-repair')
        assert code==202 and not duplicate['created'] and duplicate['job']['job_id']==jid
        assert call('/v1/jobs/'+jid)[1]['status']=='queued'
        evidence['checks']['durable_queue_and_idempotency']=True
        compose('start','worker');job=wait_job(jid)
        assert job['status']=='completed' and job['result']['output']['rows']==[['Ada'],['Grace'],['Linus']]
        evidence['checks']['registered_repair_result']=True;evidence['completed_job']=job
        code,unsafe=call('/v1/jobs',{'task_id':'employee_names','initial_sql':'DROP TABLE employees'},idem='unsafe')
        assert code==202;blocked=wait_job(unsafe['job']['job_id'])
        assert blocked['status']=='needs_review' and blocked['result']['output'] is None
        evidence['checks']['unsafe_query_not_published']=True
        assert call('/metrics',role='viewer')[0]==200
        assert call('/v1/jobs/'+jid+'/events',role='viewer')[0]==200
        evidence['checks']['metrics_and_events']=True
        if args.load_jobs:
            def trial(index):
                started=time.perf_counter()
                try:
                    code,created=call('/v1/jobs', {'task_id':'employee_names'},idem='load-'+str(index))
                    accepted=time.perf_counter()-started
                    if code != 202:
                        return {'index':index,'ok':False,'http_status':code,'seconds':accepted}
                    finished=wait_job(created['job']['job_id'])
                    ok=(finished['status']=='completed' and finished['result']['output']['rows']==[['Ada'],['Grace'],['Linus']])
                    return {'index':index,'ok':ok,'submit_seconds':accepted,
                            'seconds':time.perf_counter()-started,'job_id':created['job']['job_id']}
                except Exception as exc:
                    return {'index':index,'ok':False,'error_type':type(exc).__name__,
                            'seconds':time.perf_counter()-started}
            load_started=time.perf_counter()
            with ThreadPoolExecutor(max_workers=8) as pool:
                trials=list(pool.map(trial,range(args.load_jobs)))
            elapsed=time.perf_counter()-load_started
            times=sorted(t['seconds'] for t in trials)
            evidence['load']={'scope':'scripted registered task, no LLM or RAG; loopback HTTP + queue + worker + polling',
                'concurrency':8,'jobs':len(trials),'successes':sum(t['ok'] for t in trials),
                'failures':sum(not t['ok'] for t in trials),'elapsed_seconds':elapsed,
                'completed_jobs_per_second':sum(t['ok'] for t in trials)/elapsed,
                'p50_seconds':times[math.ceil(.5*len(times))-1],
                'p95_seconds':times[math.ceil(.95*len(times))-1],'trials':trials}
            assert all(t['ok'] for t in trials), 'load requests failed; inspect evidence'
        compose('restart','postgres','api','worker')
        address=compose('port','api','8000').strip();assert address.startswith('127.0.0.1:')
        base='http://'+address;evidence['after_restart_url']=base;wait_health()
        assert call('/v1/jobs/'+jid)[1]['result']['output']['rows']==[['Ada'],['Grace'],['Linus']]
        evidence['checks']['completed_state_survives_restart']=True
        evidence['passed']=True
        print(json.dumps({'project':project,'checks':evidence['checks'],'passed':True}),flush=True)
    except Exception as exc:
        evidence.update(passed=False,error=clean(str(exc)));raise
    finally:
        if started:
            try:
                compose('logs','--no-color','--tail','100')
                compose('down');evidence['containers_removed']=True
            except Exception as exc:evidence['cleanup_error']=clean(str(exc))
        evidence['retained_volume']=project+'_sql-jobs'
        (args.output/'evidence.json').write_text(json.dumps(evidence,indent=2))
        (args.output/'compose.log').write_text('\n'.join(logs))
        (args.output/'private-credentials.json').write_text(json.dumps({'project':project,'password':password,'keys':keys},indent=2))
        (args.output/'private-credentials.json').chmod(0o600)


if __name__=='__main__':main()
