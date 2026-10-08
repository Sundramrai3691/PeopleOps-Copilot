import json
import sqlite3

import pytest

from sql_agent.data_agent import SQLAgentPlanner, SQLAgentSession, DataAgentLoop, DataContract
from sql_agent.relational_plan import FIELDS


class Model:
    def __init__(self, plan):
        self.plan = plan
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return json.dumps(self.plan if len(self.prompts) == 1 else {
            'action': 'REPAIR', 'sql': 'SELECT id FROM items ORDER BY id'})


def run(model, contract):
    with sqlite3.connect(':memory:') as db:
        db.executescript('CREATE TABLE items(id INTEGER); INSERT INTO items VALUES(1),(2);')
        return DataAgentLoop().run('List item IDs in ascending order',
                                  SQLAgentPlanner(model, relational_plan=True),
                                  SQLAgentSession(db, contract))


def test_plan_is_traced_and_oracle_not_sent_to_either_call():
    model = Model({k: 'not applicable' for k in FIELDS})
    contract = DataContract(('id',), verification_sql='SELECT id FROM items ORDER BY id -- secret_oracle')
    result = run(model, contract)
    assert result.decision == 'KEEP'
    assert len(model.prompts) == 2
    assert all('secret_oracle' not in p for p in model.prompts)
    assert 'relational_plan' in model.prompts[1]
    event = next(t for t in result.trajectory if t['step'] == 'relational_plan')
    assert event['status'] == 'model_draft_not_verified'


@pytest.mark.parametrize('plan', [None, {}, {'grain': 'items'},
    {k: 1 for k in FIELDS}, {k: ' ' for k in FIELDS},
    {**{k: 'items' for k in FIELDS}, 'sql': 'DROP TABLE items'}])
def test_invalid_plan_stops_before_sql_generation(plan):
    model = Model(plan)
    result = run(model, DataContract(('id',)))
    assert result.decision == 'STOP'
    assert len(model.prompts) == 1
    assert not any(t['step'] == 'act_verify' for t in result.trajectory)
