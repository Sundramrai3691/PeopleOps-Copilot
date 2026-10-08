import json
import sqlite3

import pytest
from langgraph.checkpoint.memory import MemorySaver
from sql_agent.database_workflow import DatabaseWorkflow
from sql_agent.mutations import MutationPolicy, MutationService


class Reviewer:
    def __init__(self, issues):
        self.issues = issues
        self.calls = 0
    def complete_with_metadata(self, prompt):
        self.calls += 1
        return json.dumps({'issues': self.issues}), {'done_reason': 'stop'}


@pytest.mark.parametrize('mode', ['repair', 'write', 'unanchored', 'no_issue', 'unchanged'])
def test_semantic_repair_in_real_graph(tmp_path, mode):
    db = tmp_path/'data.sqlite'
    with sqlite3.connect(db) as conn:
        conn.executescript("CREATE TABLE orders(status TEXT); INSERT INTO orders VALUES ('done'),('pending');")
    service = MutationService(tmp_path/'control.sqlite', {'demo': MutationPolicy(db, ('orders',))})
    issues = [{'requirement_quote': 'completed orders', 'sql_fragment': 'FROM orders',
               'problem': 'Missing status filter', 'repair': "Use status = 'done'"}]
    if mode == 'unanchored': issues[0]['requirement_quote'] = 'made up requirement'
    if mode == 'no_issue': issues = []
    class Planner:
        semantic_repair_enabled = True
        review_model = Reviewer(issues)
        calls = 0
        def link_schema(self, *args):
            return {'schema': [('orders', 'CREATE TABLE orders(status TEXT)')]}
        def __call__(self, database_id, question, previous_sql='', error='', **kwargs):
            self.calls += 1
            if not error or mode == 'unchanged': return 'SELECT COUNT(*) FROM orders'
            assert 'Missing status filter' in error
            if mode == 'write': return 'DELETE FROM orders'
            return "SELECT COUNT(*) FROM orders WHERE status = 'done'"
        def verify_result(self, *args, **kwargs):
            # Deliberately false agreement: the audit must still run.
            return {'status': 'agreement', 'proof': False}
    planner = Planner()
    graph = DatabaseWorkflow(service).graph(MemorySaver(), planner=planner)
    state = graph.invoke(dict(id='x',database_id='demo', question="Count completed orders; completed means status = 'done'",
                              sql='', mode='auto', allow_writes=False),
                         {'configurable': {'thread_id': 'x'}})
    assert state['result']['rows'] == ([(1,)] if mode == 'repair' else [(2,)])
    assert planner.review_model.calls == 1
    assert planner.calls == (1 if mode in {'no_issue', 'unanchored'} else 2)
    assert state['attempt'] == planner.calls
    assert service._query('demo', 'SELECT COUNT(*) FROM orders')['rows'] == [(2,)]
    if mode in {'write', 'unchanged'}:
        assert state['semantic_review']['status'] == 'needs_review'
    if mode == 'repair':
        assert state['semantic_repair']['original_result']['rows'] == [(2,)]


def test_exhausted_budget_never_calls_audit_or_planner():
    from types import SimpleNamespace
    from sql_agent.semantic_repair import repair_once
    service = SimpleNamespace(policies={'demo': SimpleNamespace(engine='sqlite')})
    result = repair_once(service, object(), {'database_id': 'demo', 'generated': True,
                         'attempt': 3, 'result': {'rows': []}})
    assert result['semantic_repair']['status'] == 'skipped'
    assert 'sql' not in result
