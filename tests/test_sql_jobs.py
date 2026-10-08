import sqlite3
import pytest
from sql_agent.jobs import SqliteJobRepository


def expire(path, job_id):
    with sqlite3.connect(path) as c:
        c.execute("UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job_id,))


def test_expiration_respects_budget(tmp_path):
    path = tmp_path / 'jobs.db'
    repo = SqliteJobRepository(path)
    job, _ = repo.submit('sql_analysis', {}, 'key', max_attempts=1)
    claim = repo.claim_next('worker')
    expire(path, job.job_id)
    assert repo.claim_next('replacement') is None
    assert repo.get(job.job_id).status == 'failed'
    assert repo.get(job.job_id).attempts == 1
    with pytest.raises(RuntimeError):
        repo.finish(job.job_id, 'completed', {}, claim=claim)


def test_stale_owner_cannot_finish_or_fail_replacement(tmp_path):
    path = tmp_path / 'jobs.db'
    repo = SqliteJobRepository(path)
    job, _ = repo.submit('sql_analysis', {}, 'key')
    old = repo.claim_next('same-worker-id')
    expire(path, job.job_id)
    new = repo.claim_next('same-worker-id')
    assert new.attempts == 2
    with pytest.raises(RuntimeError):
        repo.finish(job.job_id, 'completed', {'stale': True}, claim=old)
    with pytest.raises(RuntimeError):
        repo.record_failure(job.job_id, 'error', 'stale', claim=old)
    assert repo.get(job.job_id).status == 'running'
    done = repo.finish(job.job_id, 'completed', {'current': True}, claim=new)
    assert done.result == {'current': True}


def test_operational_retry_budget(tmp_path):
    repo = SqliteJobRepository(tmp_path / 'jobs.db')
    job, _ = repo.submit('sql_analysis', {}, 'key', max_attempts=2)
    first = repo.claim_next()
    assert repo.record_failure(job.job_id, 'temporary', 'retry', claim=first).status == 'queued'
    second = repo.claim_next()
    assert repo.record_failure(job.job_id, 'temporary', 'done', claim=second).status == 'failed'


def test_expired_worker_does_not_crash_or_overwrite_new_result(tmp_path):
    from sql_agent.worker import Worker
    from sql_agent.pipeline import PipelineOutcome
    path = tmp_path / 'jobs.db'
    repo = SqliteJobRepository(path)
    job, _ = repo.submit('sql_analysis', {}, 'key')
    class SlowPipeline:
        def run(self, claimed):
            expire(path, claimed.job_id)
            current = repo.claim_next('replacement')
            repo.finish(current.job_id, 'completed', {'current': True}, claim=current)
            return PipelineOutcome('completed', {'stale': True})
    assert Worker(repo, SlowPipeline()).run_once()
    assert repo.get(job.job_id).result == {'current': True}


def test_renewal_cannot_resurrect_expired_claim(tmp_path):
    repo=SqliteJobRepository(tmp_path/'jobs.db')
    job,_=repo.submit('sql_analysis',{},'renew')
    claim=repo.claim_next('worker')
    repo.renew_lease(claim,300)
    expire(repo.path,job.job_id)
    with pytest.raises(RuntimeError):
        repo.renew_lease(claim,300)


def test_heartbeat_keeps_long_task_owned(tmp_path):
    import time
    from sql_agent.worker import Worker
    from sql_agent.pipeline import PipelineOutcome
    repo=SqliteJobRepository(tmp_path/'jobs.db')
    job,_=repo.submit('sql_analysis',{},'long')
    class Slow:
        def run(self,job):
            time.sleep(1.4)
            assert repo.claim_next('competitor') is None
            return PipelineOutcome('completed',{'ok':True})
    Worker(repo,Slow(),lease_seconds=1).run_once()
    assert repo.get(job.job_id).status=='completed'
    assert repo.get(job.job_id).attempts==1


def test_killed_worker_is_recovered_after_real_lease_expiration(tmp_path):
    import multiprocessing
    import time
    if "fork" not in multiprocessing.get_all_start_methods():
        import pytest
        pytest.skip("worker-kill scenario requires the POSIX fork start method")
    from sql_agent.worker import Worker
    from sql_agent.pipeline import JobPipeline
    path=tmp_path/'jobs.db'
    repo=SqliteJobRepository(path)
    job,_=repo.submit('sql_analysis',{'task_id':'employee_names'},'crash',max_attempts=2)
    def child():
        class Blocked:
            def run(self,job):
                time.sleep(30)
        Worker(SqliteJobRepository(path),Blocked(),lease_seconds=1).run_once()
    process=multiprocessing.get_context('fork').Process(target=child)
    process.start()
    try:
        deadline=time.monotonic()+5
        while repo.get(job.job_id).status!='running':
            assert time.monotonic()<deadline
            time.sleep(.01)
        process.kill(); process.join(timeout=5)
        time.sleep(1.2)
        assert Worker(repo,JobPipeline()).run_once()
        recovered=repo.get(job.job_id)
        assert recovered.status=='completed' and recovered.attempts==2
        assert recovered.result['output']['rows']==[['Ada'],['Grace'],['Linus']]
        assert recovered.result['telemetry']['unobserved_job_attempts'] == 1
        assert recovered.result['telemetry']['token_cost']['total_tokens'] is None
    finally:
        if process.is_alive():
            process.kill(); process.join()
