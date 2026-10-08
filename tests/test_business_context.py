from dataclasses import replace
from datetime import datetime,timezone
import sqlite3
import pytest
from sql_agent.business_context import MetricDefinition,QualityCheck,retrieve_definitions,check_source_health
from sql_agent.data_agent import SQLAgentSession,DataContract
from sql_agent.sql_config import SQLTask,SQLTaskRegistry
from sql_agent.jobs import SqliteJobRepository
from sql_agent.pipeline import JobPipeline
from sql_agent.bounded_runtime import ActionProposal

D=MetricDefinition('net_revenue',('net revenue',),'Subtract recorded refunds from paid order amounts.','commerce-policy','1')

def test_scoped_retrieval_and_required_definitions():
 assert retrieve_definitions('NET REVENUE this month',[D])[0]['metric_id']=='net_revenue'
 assert not retrieve_definitions('unrelated',[D])
 assert retrieve_definitions('unrelated',[replace(D,required=True)])
 assert not retrieve_definitions('net revenues',[D])


def test_policy_validation_and_hash_pinning():
 task=SQLTask('x','net revenue',DataContract(('x',)),definitions=(D,))
 assert task.context_sha256!=replace(task,definitions=(replace(D,version='2'),)).context_sha256
 with pytest.raises(ValueError):replace(task,definitions=(D,D))
 with pytest.raises(ValueError):QualityCheck('x','SELECT 1','violation_count',float('nan'),'blocking','test')


def test_missing_invalid_and_stale_data_fail_closed():
 db=sqlite3.connect(':memory:');session=SQLAgentSession(db,DataContract(('x',)))
 try:
  checks=(QualityCheck('stale',"SELECT '2020-01-01T00:00:00Z'",'freshness',24,'blocking','Daily ingestion'),
   QualityCheck('missing','SELECT missing FROM absent','violation_count',0,'blocking','Required source'),
   QualityCheck('nozone',"SELECT '2025-01-01'",'freshness',24,'warning','Timestamp must be explicit'))
  h=check_source_health(session,checks,now=datetime(2025,1,2,tzinfo=timezone.utc))
  assert h['blocked'] and [r['status'] for r in h['checks']]==['fail','unknown','unknown']
  assert not session.last_sql and not session.last_error
  assert check_source_health(session,())['status']=='not_configured'
 finally:db.close()


def test_quality_sql_remains_read_only_and_bounded():
 db=sqlite3.connect(':memory:');db.executescript('CREATE TABLE x(n); INSERT INTO x VALUES(1);')
 try:
  checks=(QualityCheck('write','DELETE FROM x','violation_count',0,'blocking','Do not allow writes'),)
  assert check_source_health(SQLAgentSession(db,DataContract(('x',))),checks)['blocked']
  assert db.execute('SELECT count(*) FROM x').fetchone()[0]==1
 finally:db.close()


def test_pipeline_definition_evidence_and_blocking_preflight(tmp_path):
 repo=SqliteJobRepository(tmp_path/'jobs.sqlite')
 task=SQLTask('names','List names',DataContract(('name',)),definitions=(replace(D,required=True),))
 seen=[]
 def planner(ctx):
  seen.append(ctx.goal)
  return ActionProposal('REPAIR','sql_query',{'sql':'SELECT name FROM employees ORDER BY id'})
 job,_=repo.submit('sql_analysis',{'task_id':'names','_business_context_sha256':task.context_sha256},'a')
 outcome=JobPipeline(SQLTaskRegistry((task,)),planner).run(job)
 assert D.definition in seen[0] and outcome.result['business_context']['definitions']
 assert outcome.status=='needs_review'
 blocked=replace(task,quality_checks=(QualityCheck('bad','SELECT 1','violation_count',0,'blocking','No violations allowed'),))
 with pytest.raises(ValueError,match='business context changed'):JobPipeline(SQLTaskRegistry((blocked,)),planner).run(job)
 job2,_=repo.submit('sql_analysis',{'task_id':'names'},'b');seen.clear()
 result=JobPipeline(SQLTaskRegistry((blocked,)),planner).run(job2)
 assert not seen and result.result['release']['reason']=='data_quality_blocked'
 assert result.result['candidate_output'] is None


def test_api_worker_review_has_context_and_audit(tmp_path):
 from fastapi.testclient import TestClient
 from sql_agent.api import create_app
 from sql_agent.security import ApiKeyAuthorizer,Principal
 from sql_agent.worker import Worker
 repo=SqliteJobRepository(tmp_path/'jobs.sqlite')
 task=SQLTask('names','List names',DataContract(('name',)),definitions=(replace(D,required=True),))
 registry=SQLTaskRegistry((task,));auth=ApiKeyAuthorizer({'admin':Principal('reviewer','admin'),'viewer':Principal('viewer','viewer')})
 planner=lambda ctx:ActionProposal('REPAIR','sql_query',{'sql':'SELECT name FROM employees ORDER BY id'})
 worker=Worker(repo,JobPipeline(registry,planner))
 with TestClient(create_app(repo,registry,auth)) as client:
  h={'x-api-key':'admin','idempotency-key':'a'}
  job=client.post('/v1/jobs',headers=h,json={'task_id':'names'}).json()['job']
  assert job['payload']['_business_context_sha256']==task.context_sha256
  assert worker.run_once()
  url='/v1/jobs/'+job['job_id']
  result=client.get(url,headers=h).json()
  assert result['result']['business_context']['definitions']
  assert client.post(url+'/review',headers={'x-api-key':'viewer'},json={'decision':'approve'}).status_code==403
  reviewed=client.post(url+'/review',headers=h,json={'decision':'reject','notes':'Clarify metric definition'}).json()
  assert reviewed['status']=='review_rejected'
  assert reviewed['result']['review']['notes']=='Clarify metric definition'
  page=client.get('/').text
  assert 'Business definitions' in page and 'Source health' in page


def test_preflight_and_analysis_share_snapshot(tmp_path):
 path=(tmp_path/'source.sqlite').resolve();db=sqlite3.connect(path)
 db.execute('PRAGMA journal_mode=WAL');db.executescript('CREATE TABLE items(x); INSERT INTO items VALUES(1);');db.close()
 task=SQLTask('items','List x',DataContract(('x',)),path,quality_checks=(QualityCheck('present','SELECT count(*) FROM items WHERE x IS NULL','violation_count',0,'blocking','No missing values'),))
 def planner(ctx):
  with sqlite3.connect(path) as other:other.execute('INSERT INTO items VALUES(2)')
  return ActionProposal('REPAIR','sql_query',{'sql':'SELECT x FROM items'})
 repo=SqliteJobRepository(tmp_path/'jobs.sqlite');job,_=repo.submit('sql_analysis',{'task_id':'items'},'snapshot')
 result=JobPipeline(SQLTaskRegistry((task,)),planner).run(job)
 assert result.result['source_health']['status']=='pass'
 assert result.result['candidate_output']['rows']==((1,),)
