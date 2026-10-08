import sqlite3

import pytest
from langgraph.checkpoint.memory import MemorySaver

from sql_agent.database_workflow import DatabaseWorkflow
from sql_agent.mutations import MutationPolicy, MutationService
from sql_agent.repair_feedback import repair_feedback


@pytest.fixture
def service(tmp_path):
    path = tmp_path / 'data.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript('CREATE TABLE patients(id INTEGER, diagnosis TEXT);'
                           'CREATE TABLE exams(patient_id INTEGER, hgb REAL);'
                           'CREATE TABLE secret_records(hgb REAL, private_note TEXT);'
                           "INSERT INTO patients VALUES (1, 'A');"
                           'INSERT INTO exams VALUES (1, 12);')
    return MutationService(tmp_path / 'control.sqlite', {
        'demo': MutationPolicy(path, ('patients', 'exams'))})


def test_real_graph_supplies_ownership_then_repairs(service):
    calls = []
    def planner(database_id, question, previous_sql, error):
        calls.append(error)
        if not error:
            return 'SELECT p.hgb FROM patients p'
        assert '"allowed_tables_containing_column": ["exams"]' in error
        assert '"alias": "p", "table": "patients"' in error
        assert 'secret_records' not in error and 'private_note' not in error
        return 'SELECT e.hgb FROM exams e JOIN patients p ON e.patient_id=p.id'
    graph = DatabaseWorkflow(service).graph(MemorySaver(), planner=planner)
    state = graph.invoke(dict(id='repair', database_id='demo', question='List patient HGB',
                              sql='', mode='auto', allow_writes=False),
                         {'configurable': {'thread_id': 'repair'}})
    assert state['result']['rows'] == [(12.0,)]
    assert state['attempt'] == len(calls) == 2
    assert state['rejected_proposals'][0]['repair_feedback'] == calls[1]


def test_repair_budget_remains_three(service):
    calls = []
    def planner(*args):
        calls.append(args)
        return f'SELECT missing{len(calls)} FROM patients'
    graph = DatabaseWorkflow(service).graph(MemorySaver(), planner=planner)
    with pytest.raises(sqlite3.OperationalError, match='missing3'):
        graph.invoke(dict(id='budget', database_id='demo', question='List HGB', sql='',
                          mode='auto', allow_writes=False),
                     {'configurable': {'thread_id': 'budget'}})
    assert len(calls) == 3


def test_non_column_errors_unchanged(service):
    assert repair_feedback(service.policies['demo'], 'SELECT 1', 'syntax error') == 'syntax error'
