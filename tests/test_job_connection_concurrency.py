"""Bounded process test: a native connect/close hang must fail, not hang pytest."""
import os
from pathlib import Path
import subprocess
import sys


def test_parallel_queue_reads_do_not_hang_opening_or_closing_connections(tmp_path):
    code='''
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from sql_agent.jobs import SqliteJobRepository
repo=SqliteJobRepository(Path(sys.argv[1]))
job,_=repo.submit('sql_analysis',{'task_id':'employee_names'},'parallel-read')
with ThreadPoolExecutor(max_workers=4) as pool:
    rows=list(pool.map(lambda _:repo.get(job.job_id).status,range(400)))
assert rows==['queued']*400
print('400 concurrent queue reads passed')
'''
    result=subprocess.run([sys.executable,'-c',code,str(tmp_path/'queue.sqlite')],
                          env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')},
                          capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert '400 concurrent queue reads passed' in result.stdout
