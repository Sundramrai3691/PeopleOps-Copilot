import json
import pytest
from scripts.evaluation.run_paired_sql_benchmark import run_episode
from scripts.evaluation.transfer_sql_cases import CASES, fixture, create_database
from scripts.evaluation import run_paired_sql_benchmark as runner
from scripts.evaluation.transfer_sql_cases import expected


@pytest.mark.parametrize('response,accepted', [
    ('```sql\nSELECT id AS invoice_id FROM invoices WHERE id = 14 ORDER BY id\n```', True),
    ('```sql\nSELECT 1; DELETE FROM invoices;\n```', False),
])
def test_native_pipeline_executes_only_authorized_sql(tmp_path, monkeypatch, response, accepted):
    monkeypatch.setattr(runner,'expected',expected)
    dbs={};data={}
    for v in range(2):
        data[v]=fixture(v);dbs[v]=tmp_path/f'{v}.sqlite';create_database(dbs[v],data[v])
    class Model:
        model='test'
        def complete_with_metadata(self,prompt):
            return response,{'prompt_tokens':10,'completion_tokens':20,'done_reason':'stop'}
    case=next(c for c in CASES if c.case_id=='no_settled_receipt')
    row=run_episode(case,1,'native_sql','clean',0,None,dbs,data,Model())
    assert row['accepted_correct'] is accepted
    assert row['source_unchanged'] and not row['auto_released']
    assert len(row['calls'])==1


def test_native_execution_error_repairs_without_offline_oracle(tmp_path,monkeypatch):
    monkeypatch.setattr(runner,'expected',expected)
    dbs={};data={}
    for v in range(2):
        data[v]=fixture(v);dbs[v]=tmp_path/f'{v}.sqlite';create_database(dbs[v],data[v])
    prompts=[]
    class Model:
        model='test'
        def complete_with_metadata(self,prompt):
            prompts.append(prompt)
            sql=('SELECT invoice_id FROM invoices WHERE id=14 ORDER BY id' if len(prompts)==1
                 else 'SELECT id AS invoice_id FROM invoices WHERE id=14 ORDER BY id')
            return sql,{'prompt_tokens':10,'completion_tokens':20,'done_reason':'stop'}
    case=next(c for c in CASES if c.case_id=='no_settled_receipt')
    row=run_episode(case,1,'native_sql_repair','clean',0,None,dbs,data,Model())
    assert row['first_query_correct'] is False and row['accepted_correct']
    assert len(prompts)==2 and 'no such column: invoice_id' in prompts[1]
    assert 'verification_sql' not in ''.join(prompts)
    assert not row['auto_released'] and row['source_unchanged']
    assert row['pipeline']['status']=='needs_review'


def test_executable_wrong_answer_is_not_retried_using_hidden_oracle(tmp_path,monkeypatch):
    monkeypatch.setattr(runner,'expected',expected)
    dbs={};data={}
    for v in range(2):
        data[v]=fixture(v);dbs[v]=tmp_path/f'{v}.sqlite';create_database(dbs[v],data[v])
    class Model:
        model='test'
        def complete_with_metadata(self,prompt):
            return 'SELECT id AS invoice_id FROM invoices ORDER BY id',{}
    case=next(c for c in CASES if c.case_id=='no_settled_receipt')
    row=run_episode(case,1,'native_sql_repair','clean',0,None,dbs,data,Model())
    assert row['accepted_incorrect'] and len(row['calls'])==1
    assert not row['auto_released'] and row['pipeline']['status']=='needs_review'
