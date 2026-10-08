"""Versioned dataset boundary: reject unsafe fixtures and cross-split leakage."""
import pytest
from sql_agent.training_dataset import prepare_dataset, execute_read, score_response


def record(ident, domain, context='CREATE TABLE sales(amount INT); INSERT INTO sales VALUES (10);'):
    return {'id':ident, 'domain':domain, 'sql_context':context,
            'sql_prompt':f'Get amount for {domain}', 'sql':'SELECT amount FROM sales'}


def test_dataset_partitions_are_domain_disjoint_and_sources_not_executable_files():
    rows = [record(i, f'domain{i}', f'CREATE TABLE sales{i}(amount INT); INSERT INTO sales{i} VALUES (10);')
            for i in range(30)]
    for i, row in enumerate(rows):
        row['sql'] = f'SELECT amount FROM sales{i}'
    rows.append(record(99,'unsafe', "ATTACH '/tmp/not-allowed' AS external;"))
    dataset = prepare_dataset(rows, limits={'train':10,'dev':3,'test':5})
    splits = dataset['splits']
    assert all(splits.values())
    domains = {k:{r['domain'] for r in v} for k,v in splits.items()}
    assert not domains['train'] & domains['test']
    assert not domains['train'] & domains['dev']
    assert all(r['id'] != 99 for split in splits.values() for r in split)
    assert all(r['expected_rows'] == [[10]] for split in splits.values() for r in split)


def test_evaluation_read_cannot_modify_or_attach():
    context = 'CREATE TABLE sales(amount INT); INSERT INTO sales VALUES (10);'
    with pytest.raises(ValueError):
        execute_read(context, 'DELETE FROM sales')
    with pytest.raises(ValueError):
        execute_read(context, "SELECT load_extension('/tmp/malicious')")


def test_scoring_matches_runtime_fence_handling_and_separates_format():
    row = record(1, 'sales')
    row['expected_rows'] = [[10]]
    score = score_response(row, '```json\n{"sql":"SELECT amount FROM sales"}\n```')
    assert score['correct'] is True
    assert score['strict_json'] is False
    assert score_response(row, '{"sql":"SELECT amount+1 FROM sales"}')['correct'] is False
