"""Run regressions against a disposable, loopback-only PostgreSQL container."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    name='sql-agent-pg-test-'+uuid.uuid4().hex[:12]
    password=secrets.token_hex(24)
    evidence={'container':name,'scope':'disposable PostgreSQL regression; not production traffic'}
    started=False
    def docker(*parts):
        return subprocess.check_output(['docker',*parts],text=True,stderr=subprocess.STDOUT).strip()
    try:
        docker('run','-d','--name',name,'--tmpfs','/var/lib/postgresql/data',
               '-p','127.0.0.1::5432','-e','POSTGRES_USER=sqltest','-e','POSTGRES_DB=sqltest',
               '-e','POSTGRES_PASSWORD='+password,'postgres:16-alpine')
        started=True
        address=docker('port',name,'5432/tcp')
        if not address.startswith('127.0.0.1:'):
            raise RuntimeError('test database must be loopback-only')
        dsn='postgresql://sqltest:'+password+'@'+address+'/sqltest'
        import psycopg
        deadline=time.monotonic()+30
        while True:
            try:
                with psycopg.connect(dsn,connect_timeout=1):
                    break
            except psycopg.OperationalError:
                if time.monotonic()>deadline:
                    raise RuntimeError('test PostgreSQL unavailable')
                time.sleep(.2)
        env=os.environ.copy()
        env.update(SQL_AGENT_TEST_POSTGRES_DSN=dsn,PYTHONPATH='src:.',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
        result=subprocess.run([sys.executable,'-m','pytest','-q','--junitxml='+str(args.output/'regression.xml')],
                              env=env,text=True,capture_output=True,timeout=240)
        log=(result.stdout+result.stderr).replace(password,'<REDACTED>')
        (args.output/'pytest.log').write_text(log)
        evidence['exit_code']=result.returncode
        print(log,flush=True)
    finally:
        if started:
            docker('rm','-f',name)
            evidence['temporary_container_and_tmpfs_removed']=True
        (args.output/'evidence.json').write_text(json.dumps(evidence,indent=2))
    if evidence.get('exit_code'):
        raise SystemExit(evidence['exit_code'])


if __name__=='__main__':
    main()
