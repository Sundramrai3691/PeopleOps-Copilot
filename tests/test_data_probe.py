import json
import sqlite3

import pytest

from sql_agent.data_agent import SQLAgentPlanner, SQLAgentSession, DataAgentLoop, DataContract
from sql_agent.data_probe import execute_probes


@pytest.fixture
def db():
    connection = sqlite3.connect(':memory:')
    connection.executescript("CREATE TABLE events(id INTEGER, kind TEXT); INSERT INTO events VALUES(1,'note'),(1,'work'),(2,NULL);")
    yield connection
    connection.close()


def test_probes_denied_writes_and_external_reads_leave_source_unchanged(db):
    result = execute_probes(db, ['DELETE FROM events', "SELECT load_extension('/tmp/probe')"])
    assert result[0]['status'] == 'denied'
    assert result[1]['status'] == 'error'
    assert db.execute('SELECT COUNT(*) FROM events').fetchone() == (3,)


def test_probe_budget_and_truncation(db):
    with pytest.raises(ValueError):
        execute_probes(db, ['SELECT 1'] * 3)
    db.executemany('INSERT INTO events VALUES(?,?)', [(n, 'x'*200) for n in range(20)])
    result = execute_probes(db, ['SELECT * FROM events'])[0]
    assert result['truncated'] and len(result['rows']) == 8
    assert len(result['rows'][-1][1]) == 160


def test_observation_enters_generation_but_never_reference_answer(db):
    class Model:
        def __init__(self):
            self.prompts = []

        def complete(self, prompt):
            self.prompts.append(prompt)
            assert 'secret_reference' not in prompt
            if len(self.prompts) == 1:
                return json.dumps({'queries': ['SELECT kind, COUNT(*) FROM events GROUP BY kind']})
            assert 'observations' in prompt and 'note' in prompt and 'work' in prompt
            return json.dumps({'action': 'REPAIR', 'sql': 'SELECT COUNT(*) AS total FROM events'})

    model = Model()
    contract = DataContract(('total',), verification_sql='SELECT COUNT(*) AS total FROM events -- secret_reference')
    result = DataAgentLoop().run('Count all events', SQLAgentPlanner(model, data_probe=True),
                                 SQLAgentSession(db, contract))
    assert result.decision == 'KEEP' and len(model.prompts) == 2
    assert db.in_transaction  # snapshot remains active through final execution
    event = next(t for t in result.trajectory if t['step'] == 'data_probe')
    assert event['observations'][0]['status'] == 'ok'
    assert sum(t['step'] == 'propose' for t in result.trajectory) == 1


@pytest.mark.parametrize('requests', [None, {'queries': ['SELECT 1']*3}, {'queries': [3]}, {'queries': [], 'sql':'SELECT 1'}])
def test_malformed_requests_stop_before_execution(db, requests):
    class Model:
        def complete(self, prompt):
            return json.dumps(requests)
    result = DataAgentLoop().run('Count events', SQLAgentPlanner(Model(), data_probe=True),
                                 SQLAgentSession(db, DataContract(('total',))))
    assert result.decision == 'STOP'
    assert not any(t['step'] == 'act_verify' for t in result.trajectory)
