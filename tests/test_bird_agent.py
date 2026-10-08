import json
import sqlite3

import pytest
from langgraph.checkpoint.memory import MemorySaver
from scripts.evaluation.evaluate_bird_agent import BirdPlanner, summarize, configuration_matches
from sql_agent.database_workflow import DatabaseWorkflow
from sql_agent.mutations import MutationPolicy, MutationService
from sql_agent.retrieval import KnowledgeRetriever


class Model:
    def __init__(self, answers):
        self.answers=iter(answers)
        self.prompts=[]
    def complete_with_metadata(self,prompt):
        self.prompts.append(prompt)
        return next(self.answers), {'done_reason':'stop'}
    def complete(self,prompt):
        return self.complete_with_metadata(prompt)[0]


@pytest.mark.parametrize("profile", ["slonik", "xiyan", "generic"])
def test_real_graph_repairs_and_independently_verifies(tmp_path, profile):
    path=tmp_path/'data.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE things(value INTEGER)')
        db.execute('INSERT INTO things VALUES (7)')
    service=MutationService(tmp_path/'control.sqlite',{'demo':MutationPolicy(path,('things',),max_rows=10000)})
    primary=Model(['SELECT missing FROM things','SELECT value FROM things'])
    reviewer=Model([json.dumps({'action':'REPAIR','sql':'SELECT value FROM things'})])
    planner=BirdPlanner(service,primary,reviewer,KnowledgeRetriever(),profile=profile)
    graph=DatabaseWorkflow(service).graph(MemorySaver(),planner=planner)
    state=graph.invoke(dict(id='test',database_id='demo',question='List each value from things',sql='',mode='auto',allow_writes=False),{'configurable':{'thread_id':'test'}})
    assert state['result']['rows']==[(7,)]
    assert state['attempt']==2
    assert state['semantic_review']['status']=='agreement'
    assert 'no such column' in primary.prompts[1]
    assert 'SELECT missing' not in reviewer.prompts[0]


def test_paired_metrics_and_resume_guard():
    records=[dict(question_id=1,db_id='d',correct=True,status='correct',review={'status':'agreement'}),
             dict(question_id=2,db_id='d',correct=False,status='wrong',review={'status':'agreement'})]
    s=summarize(records,{'1':{'correct':False},'2':{'correct':True}})
    assert s['recovered_ids']==[1] and s['regressed_ids']==[2]
    assert s['false_accept_among_agreements']==0.5
    assert s['accuracy_500'] is None
    with pytest.raises(ValueError): configuration_matches({'model':'old'},{'model':'new'})


def test_read_functions_allowed_without_allowing_writes_or_unlisted_tables(tmp_path):
    path=tmp_path/'data.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE things(value INTEGER)')
        db.execute('INSERT INTO things VALUES (-7)')
        db.execute('CREATE TABLE hidden(secret TEXT)')
    service=MutationService(tmp_path/'control.sqlite',{'demo':MutationPolicy(path,('things',),max_rows=10000)})
    result=service._query('demo', "SELECT abs(value), nullif(value,0), group_concat(value), date('2026-01-01'), current_timestamp FROM things WHERE 'abc' LIKE 'a%'")
    assert result['rows'][0][:4] == (7,-7,'-7','2026-01-01')
    for sql in ["DELETE FROM things", "SELECT * FROM hidden", "SELECT load_extension('bad')"]:
        with pytest.raises((ValueError,sqlite3.DatabaseError)):
            service._query('demo',sql)
    assert service._query('demo','SELECT value FROM things')['rows']==[(-7,)]


def test_full_schema_no_rag_matches_baseline_prompt(tmp_path):
    from scripts.evaluation.sql_model_profiles import prompt_for
    from scripts.evaluation.evaluate_bird_single_pass import execute
    path=tmp_path/'data.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE things(value INTEGER)')
    service=MutationService(tmp_path/'control.sqlite',{'demo':MutationPolicy(path,('things',))})
    primary=Model(['SELECT value FROM things'])
    planner=BirdPlanner(service,primary,Model([]),KnowledgeRetriever(),profile='xiyan',
        schema_mode='full',use_rag=False,evidence='official hint',original_question='List values')
    context=planner.retrieve('demo','List values')
    schema=planner.link_schema('demo','List values',context)
    planner('demo','List values',context=context,schema_context=schema)
    ddl=execute(path,"SELECT sql FROM sqlite_master WHERE name='things'")[0][0]
    assert primary.prompts[0]==prompt_for('xiyan',ddl,'List values','official hint')
    assert context['status']=='disabled'
    result=service._query('demo',"SELECT iif(1,7,0), instr('abc','b')")
    assert result['rows']==[(7,2)]


@pytest.mark.parametrize('operator', ['INTERSECT','UNION','EXCEPT'])
def test_set_queries_route_as_reads_and_large_limit_reaches_verifier(tmp_path, operator):
    path=tmp_path/'data.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE things(value INTEGER)')
        db.execute('INSERT INTO things VALUES (7)')
    service=MutationService(tmp_path/'control.sqlite',{'demo':MutationPolicy(path,('things',),max_rows=100000)})
    sql=f'SELECT value FROM things {operator} SELECT value FROM things'
    primary=Model([sql]);reviewer=Model([json.dumps({'action':'REPAIR','sql':sql})])
    planner=BirdPlanner(service,primary,reviewer,KnowledgeRetriever())
    graph=DatabaseWorkflow(service).graph(MemorySaver(),planner=planner)
    state=graph.invoke(dict(id='set-query',database_id='demo',question='Get values',sql='',mode='auto',allow_writes=False),{'configurable':{'thread_id':'set-query'}})
    assert state['mode']=='query'
    assert state['semantic_review']['status']=='agreement'
    assert len(reviewer.prompts)==1


def test_benchmark_row_limit_above_ten_thousand(tmp_path):
    path=tmp_path/'data.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE things(value INTEGER)')
        db.executemany('INSERT INTO things VALUES (?)', ((i,) for i in range(10001)))
    service=MutationService(tmp_path/'control.sqlite',{'demo':MutationPolicy(path,('things',),max_rows=100000)})
    result=service._query('demo','SELECT value FROM things')
    assert len(result['rows'])==10001 and not result['truncated']
    with pytest.raises(ValueError): service._query('demo','DELETE FROM things')
