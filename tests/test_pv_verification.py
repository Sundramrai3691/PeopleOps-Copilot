import sqlite3
import pytest
from sql_agent.pv_verification import propose, verify_and_repair
from sql_agent.mutations import MutationPolicy, MutationService


@pytest.mark.parametrize('sql,evidence', [
    ("SELECT * FROM disp d WHERE d.type = 'OWNER'", "eligible means disp.type = 'OWNER'"),
    ("SELECT MAX(amount) FROM disp", 'highest amount'),
    ("SELECT * FROM disp WHERE type IN ('OWNER','USER')", "disp.type = 'OWNER'"),
    ("SELECT * FROM disp WHERE type = 'USER' OR id = 1", "disp.type = 'OWNER'"),
    ("SELECT * FROM disp WHERE type = 'USER'", "type = 'OWNER'"),
    ("SELECT * FROM disp WHERE type = 'USER'", "disp.type = 'OWNER'; disp.type = 'OTHER'"),
    ("SELECT * FROM disp WHERE type = 'USER'", "hidden.type = 'OWNER'"),
    ("SELECT * FROM disp WHERE type = 'USER' AND type = 'OTHER'", "disp.type = 'OWNER'"),
])
def test_abstain(sql, evidence):
    assert propose(sql, evidence, ('disp',))['status'] != 'candidate'


def test_exact_alias_binding_only():
    event = propose("SELECT d.id FROM disp d WHERE d.type = 'USER' AND d.id > 5",
                    "eligible means disp.type = 'OWNER'", ('disp',))
    assert event['status'] == 'candidate'
    assert "d.type = 'OWNER'" in event['sql'] and 'd.id > 5' in event['sql']
    assert len(event['changes']) == 1


def test_execution_and_preservation(tmp_path):
    db = tmp_path/'db.sqlite'
    with sqlite3.connect(db) as c:
        c.executescript("CREATE TABLE disp(id INT, type TEXT); INSERT INTO disp VALUES (1,'OWNER'),(2,'USER');")
    service = MutationService(tmp_path/'control.sqlite', {'demo': MutationPolicy(db, ('disp',))})
    state = dict(database_id='demo', generated=True, sql="SELECT id FROM disp WHERE type='USER'",
                 result={'rows': [(2,)], 'columns': ['id'], 'truncated': False},
                 question="List eligible IDs\nProvided BIRD evidence: eligible means disp.type = 'OWNER'",
                 semantic_review={'status': 'agreement'})
    updated = verify_and_repair(service, state)
    assert updated['result']['rows'] == [(1,)]
    assert updated['semantic_repair']['original_result']['rows'] == [(2,)]
    assert updated['semantic_review']['status'] == 'needs_review'
    assert state['result']['rows'] == [(2,)]


def test_graph_routes_opt_in_without_model_critique(tmp_path):
    from langgraph.checkpoint.memory import MemorySaver
    from sql_agent.database_workflow import DatabaseWorkflow
    db = tmp_path/'db.sqlite'
    with sqlite3.connect(db) as conn:
        conn.executescript("CREATE TABLE disp(type TEXT); INSERT INTO disp VALUES ('OWNER');")
    service = MutationService(tmp_path/'control.sqlite', {'demo': MutationPolicy(db, ('disp',))})
    class Planner:
        pv_verification_enabled = True
        calls = 0
        def __call__(self, *args):
            self.calls += 1
            return "SELECT type FROM disp WHERE type='USER'"
        def verify_result(self, *args):
            return {'status': 'agreement', 'proof': False}
    planner = Planner()
    state = DatabaseWorkflow(service).graph(MemorySaver(), planner=planner).invoke(
        dict(id='pv', database_id='demo', sql='', mode='auto', allow_writes=False,
             question="List owners\nProvided BIRD evidence: disp.type = 'OWNER'"),
        {'configurable': {'thread_id': 'pv'}})
    assert state['result']['rows'] == [('OWNER',)]
    assert state['semantic_repair']['status'] == 'repaired'
    assert planner.calls == 1
    assert state['semantic_review']['status'] == 'needs_review'
