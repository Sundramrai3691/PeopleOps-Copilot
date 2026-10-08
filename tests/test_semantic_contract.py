import sqlite3
import pytest
from sql_agent.data_agent import DataContract, SQLAgentSession, DataAgentLoop
from sql_agent.bounded_runtime import ActionProposal


def test_wrong_business_answer_cannot_be_accepted():
    db=sqlite3.connect(':memory:')
    db.executescript('CREATE TABLE orders(id INTEGER, amount INTEGER); INSERT INTO orders VALUES(1,100),(2,200);')
    contract=DataContract(('total',), verification_sql='SELECT SUM(amount) AS total FROM orders')
    try:
        result=DataAgentLoop().run('total', lambda ctx: ActionProposal('REPAIR','sql_query',{'sql':'SELECT COUNT(*) AS total FROM orders'}), SQLAgentSession(db,contract))
        assert result.decision == 'STOP'
        assert result.output is None
    finally:
        db.close()


def test_semantic_feedback_repairs_on_current_data():
    db=sqlite3.connect(':memory:')
    db.executescript('CREATE TABLE orders(amount INTEGER); INSERT INTO orders VALUES(7),(11);')
    contract=DataContract(('total',), verification_sql='SELECT SUM(amount) AS total FROM orders')
    def planner(ctx):
        if ctx.attempt == 2:
            assert ctx.evidence.previous_sql == 'SELECT COUNT(*) AS total FROM orders'
            assert ctx.evidence.previous_error == 'business_result_mismatch'
        return ActionProposal('REPAIR','sql_query',{'sql':'SELECT COUNT(*) AS total FROM orders' if ctx.attempt==1 else 'SELECT SUM(amount) AS total FROM orders'})
    try:
        result=DataAgentLoop().run('total',planner,SQLAgentSession(db,contract))
        assert result.decision=='KEEP'
        assert result.output['rows']==((18,),)
        assert result.reason=='business_contract_verified'
    finally:
        db.close()


@pytest.mark.parametrize('check', ['DELETE FROM orders','SELECT missing FROM orders','SELECT amount AS total FROM orders UNION ALL SELECT amount AS total FROM orders'])
def test_bad_or_oversized_verifier_fails_closed(check):
    db=sqlite3.connect(':memory:')
    db.executescript('CREATE TABLE orders(amount INTEGER); INSERT INTO orders VALUES(7),(11);')
    try:
        contract=DataContract(('total',),max_rows=2,verification_sql=check)
        r=DataAgentLoop().run('total',lambda ctx: ActionProposal('REPAIR','sql_query',{'sql':'SELECT 18 AS total FROM orders LIMIT 1'}),SQLAgentSession(db,contract))
        assert r.decision=='STOP' and r.output is None
        assert db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==2
    finally:
        db.close()


def test_verifier_sql_not_sent_to_model_and_changes_hash():
    from sql_agent.data_agent import SQLAgentPlanner
    class Model:
        def complete(self,prompt):
            assert 'SUM(amount)' not in prompt
            return '{"action":"STOP"}'
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE orders(amount INTEGER)')
    a=DataContract(('total',),verification_sql='SELECT SUM(amount) AS total FROM orders')
    b=DataContract(('total',),verification_sql='SELECT COUNT(*) AS total FROM orders')
    assert a.snapshot().sha256 != b.snapshot().sha256
    try:
        DataAgentLoop().run('total',SQLAgentPlanner(Model()),SQLAgentSession(db,a))
    finally:
        db.close()


def test_question_cannot_escape_business_contract():
    from sql_agent.sql_config import SQLTask
    task=SQLTask('revenue','January revenue',DataContract(('total',),verification_sql='SELECT 1 AS total'))
    task.validate_question(None)
    task.validate_question('January revenue')
    with pytest.raises(ValueError):
        task.validate_question('March revenue')


def test_verification_uses_same_read_snapshot(tmp_path):
    p=tmp_path/'source.sqlite'
    reader=sqlite3.connect(p)
    reader.execute('PRAGMA journal_mode=WAL')
    reader.executescript('CREATE TABLE orders(amount INTEGER); INSERT INTO orders VALUES(7),(11);')
    writer=sqlite3.connect(p)
    session=SQLAgentSession(reader,DataContract(('total',),verification_sql='SELECT SUM(amount) AS total FROM orders'))
    proposal=ActionProposal('REPAIR','sql_query',{'sql':'SELECT SUM(amount) AS total FROM orders'})
    try:
        output=session.execute(proposal)
        writer.execute('INSERT INTO orders VALUES(100)'); writer.commit()
        assert session.verify(proposal,output)==(True,'business_contract_verified')
        assert output['rows']==((18,),)
        assert writer.execute('SELECT SUM(amount) FROM orders').fetchone()[0]==118
    finally:
        reader.close(); writer.close()


def test_saved_wrong_customer_and_date_queries_are_rejected():
    import json
    from pathlib import Path
    root=Path(__file__).parents[1]
    cases=json.loads((root/'data/benchmarks/commerce_v1.json').read_text())
    queries=json.loads((root/'tests/fixtures/commerce_wrong_queries.json').read_text())['queries']
    for name in ('customer_net','empty_cohort'):
        case=next(c for c in cases if c['id']=='commerce:'+name)
        sql=queries[name]
        db=sqlite3.connect(':memory:'); db.executescript(case['setup_sql'])
        try:
            r=DataAgentLoop().run(case['goal'],lambda ctx: ActionProposal('REPAIR','sql_query',{'sql':sql}),SQLAgentSession(db,DataContract(**case['contract'],verification_sql=case['gold_sql'])))
            assert r.decision=='STOP' and r.output is None
        finally:
            db.close()


def test_explicit_catalog_fallback_is_audited_not_model_success():
    db=sqlite3.connect(':memory:')
    db.executescript('CREATE TABLE orders(amount INTEGER); INSERT INTO orders VALUES(7),(11);')
    try:
        c=DataContract(('total',),verification_sql='SELECT SUM(amount) AS total FROM orders',fallback_to_verified_query=True)
        r=DataAgentLoop().run('total',lambda ctx: ActionProposal('REPAIR','sql_query',{'sql':'SELECT COUNT(*) AS total FROM orders'}),SQLAgentSession(db,c))
        assert r.reason=='registered_business_fallback_verified'
        assert r.output['rows']==((18,),)
        assert any(t['step']=='catalog_fallback' for t in r.trajectory)
    finally:
        db.close()


def test_write_denial_never_triggers_catalog_fallback():
    db=sqlite3.connect(':memory:'); db.execute('CREATE TABLE orders(amount INTEGER)')
    try:
        c=DataContract(('total',),verification_sql='SELECT COUNT(*) AS total FROM orders',fallback_to_verified_query=True)
        r=DataAgentLoop().run('total',lambda ctx: ActionProposal('REPAIR','sql_query',{'sql':'DELETE FROM orders'}),SQLAgentSession(db,c))
        assert r.decision=='STOP' and r.reason=='read_only_policy_violation'
        assert not any(t['step']=='catalog_fallback' for t in r.trajectory)
    finally:
        db.close()


def test_real_model_http_timeout_uses_explicit_catalog_fallback():
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from sql_agent.data_agent import SQLAgentPlanner
    from sql_agent.planner import OllamaPlannerModel
    class SlowHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            time.sleep(.3)
            self.close_connection=True
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(('127.0.0.1',0),SlowHandler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    db=sqlite3.connect(':memory:')
    db.executescript('CREATE TABLE orders(amount INTEGER); INSERT INTO orders VALUES(7),(11);')
    try:
        planner=SQLAgentPlanner(OllamaPlannerModel(f'http://127.0.0.1:{server.server_port}','test',timeout=.05))
        contract=DataContract(('total',),verification_sql='SELECT SUM(amount) AS total FROM orders',fallback_to_verified_query=True)
        result=DataAgentLoop().run('total',planner,SQLAgentSession(db,contract))
        assert result.reason=='registered_business_fallback_verified'
        assert result.output['rows']==((18,),)
        event=next(t for t in result.trajectory if t['step']=='catalog_fallback')
        assert event['trigger'].startswith('planner_error:')
    finally:
        db.close(); server.shutdown(); server.server_close();thread.join()
