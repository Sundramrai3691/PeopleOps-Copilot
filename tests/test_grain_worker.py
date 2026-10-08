import sqlite3
import pytest
from sql_agent.bounded_runtime import ActionProposal
from sql_agent.data_agent import DataContract
from sql_agent.jobs import SqliteJobRepository
from sql_agent.pipeline import JobPipeline
from sql_agent.sql_config import SQLTask, SQLTaskRegistry
from sql_agent.worker import Worker

BAD = "SELECT p.sku,COALESCE(SUM(m.delta),0)-COALESCE(SUM(h.units),0) AS available FROM products p LEFT JOIN movements m ON m.sku_id=p.sku_id LEFT JOIN holds h ON h.sku_id=p.sku_id GROUP BY p.sku"
SAFE = "SELECT p.sku,COALESCE((SELECT SUM(m.delta) FROM movements m WHERE m.sku_id=p.sku_id),0)-COALESCE((SELECT SUM(h.units) FROM holds h WHERE h.sku_id=p.sku_id),0) AS available FROM products p"


def execute_job(tmp_path, sql, checker=None, drop_key=False, nullable_key=False):
    path=tmp_path/'source.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE products(sku_id INTEGER PRIMARY KEY,sku TEXT); CREATE TABLE movements(move_id INTEGER PRIMARY KEY,sku_id INTEGER,delta INTEGER); CREATE TABLE holds(hold_id INTEGER PRIMARY KEY,sku_id INTEGER,units INTEGER); INSERT INTO products VALUES(1,"A"); INSERT INTO movements VALUES(1,1,30),(2,1,30); INSERT INTO holds VALUES(1,1,4),(2,1,2);')
        if drop_key:
            db.executescript('ALTER TABLE holds RENAME TO old_holds; CREATE TABLE holds AS SELECT * FROM old_holds;')
        if nullable_key:
            db.executescript('ALTER TABLE holds RENAME TO old_holds; CREATE TABLE holds(hold_id INTEGER PRIMARY KEY DESC,sku_id INTEGER,units INTEGER); INSERT INTO holds SELECT * FROM old_holds;')
    contract=DataContract(('sku','available'), grain_measures=(('movements','delta','move_id'),('holds','units','hold_id')))
    registry=SQLTaskRegistry((SQLTask('stock','Available units',contract,path),))
    repo=SqliteJobRepository(tmp_path/'jobs.sqlite');job,_=repo.submit('sql_analysis',{'task_id':'stock'},'case')
    primary=lambda ctx:ActionProposal('REPAIR','sql_query',{'sql':sql})
    review=lambda ctx:ActionProposal('REPAIR','sql_query',{'sql':checker or sql})
    assert Worker(repo,JobPipeline(registry,primary,max_attempts=1,reviewer=review)).run_once()
    return repo.get(job.job_id)


def test_same_wrong_join_is_blocked_before_agreement(tmp_path):
    result=execute_job(tmp_path,BAD)
    assert result.result['semantic_review']['status']=='grain_blocked'
    assert result.result['semantic_review']['grain']['status']=='unproven'
    assert result.status=='needs_review'
    assert result.result['output'] is None


def test_separate_aggregates_remain_usable(tmp_path):
    result=execute_job(tmp_path,SAFE)
    assert result.result['semantic_review']['status']=='agreement'
    assert result.result['candidate_output']['rows']==[['A',54]]
    assert result.result['semantic_review']['grain']['candidate']['status']=='proven'
    assert result.status=='needs_review'


def test_legal_many_to_one_join_remains_usable(tmp_path):
    sql="SELECT p.sku,(SELECT SUM(m.delta) FROM movements m JOIN products d ON d.sku_id=m.sku_id WHERE d.sku_id=p.sku_id)-(SELECT SUM(h.units) FROM holds h WHERE h.sku_id=p.sku_id) AS available FROM products p"
    result=execute_job(tmp_path,sql)
    assert result.result['semantic_review']['status']=='agreement'
    assert result.result['candidate_output']['rows']==[['A',54]]


def test_checker_is_subject_to_same_grain_constraint(tmp_path):
    result=execute_job(tmp_path,SAFE,BAD)
    assert result.result['semantic_review']['status']=='grain_blocked'
    assert result.result['semantic_review']['blocked_side']=='checker'


@pytest.mark.parametrize('sql',[
    BAD.replace('SUM(m.delta)','SUM(DISTINCT m.delta)'),
    BAD.replace('h.sku_id=p.sku_id','h.sku_id=p.sku_id OR h.units=2'),
    SAFE.replace('SUM(m.delta)','SUM(m.delta*2)'),
])
def test_unproven_shapes_fail_closed(tmp_path,sql):
    result=execute_job(tmp_path,sql)
    assert result.result['semantic_review']['status']=='grain_blocked'
    assert result.result['output'] is None


def test_declaration_alone_does_not_prove_uniqueness(tmp_path):
    result=execute_job(tmp_path,SAFE,drop_key=True)
    assert result.result['semantic_review']['status']=='grain_blocked'
    assert result.result['semantic_review']['grain']['reason']=='registered_grain_not_proven_by_live_primary_key'


def test_nullable_integer_primary_key_is_not_a_rowid_guarantee(tmp_path):
    result=execute_job(tmp_path,SAFE,nullable_key=True)
    assert result.result['semantic_review']['status']=='grain_blocked'
