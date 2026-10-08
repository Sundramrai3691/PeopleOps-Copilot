import json

import pytest

from sql_agent.paired_benchmark import CASES, create_database, expected, fixture
from scripts.evaluation.run_paired_sql_benchmark import (CALL_LIMIT, CallBudgetExceeded, ObservedModel,
                                            paired_difference, run_episode, score_sql, summarize)


@pytest.mark.parametrize('domain,data,answers', [
    ('inventory', {'items': [(1,'a'),(2,'b'),(3,'c')], 'stock': [(1,'east',10),(1,'west',20)],
                   'allocations': [(1,5,'active'),(1,2,'active'),(2,90,'cancelled')]},
     {'stock_by_item': [[1,30],[2,0],[3,0]], 'available_units': [[23]],
      'unallocated_items': [[2],[3]], 'east_stock': [[1,10],[2,0],[3,0]]}),
    ('support', {'tickets': [(1,'a',None),(2,'a','2026-03-01'),(3,'b','2026-04-01'),(4,'c','2026-03-31')],
                 'events': [(1,'work',5),(1,'work',7),(1,'note',100),(3,'note',10)]},
     {'open_by_team': [['a',1],['b',0],['c',0]], 'work_by_team': [['a',12],['b',0],['c',0]],
      'unworked_tickets': [[2],[3],[4]], 'march_closed': [[2]]}),
    ('shipping', {'parcels': [(1,'n',100),(2,'n',200),(3,'s',300),(4,'w',400)],
                  'scans': [(1,'delivered','2026-03-01'),(1,'delivered','2026-03-02'),
                            (2,'delivered','2026-04-01'),(3,'transit','2026-03-12')]},
     {'delivered_weight': [[300]], 'delivered_by_region': [['n',2],['s',0],['w',0]],
      'undelivered_ids': [[3],[4]], 'march_deliveries': [[1]]}),
])
def test_oracles_against_hand_calculated_edge_examples(domain, data, answers):
    for case in CASES:
        if case.domain == domain:
            assert expected(case, data) == {'columns': list(case.columns), 'rows': answers[case.case_id]}


def sources(tmp_path, case):
    databases, data = {}, {}
    for variant in range(2):
        data[variant] = fixture(case.domain, variant)
        databases[variant] = tmp_path/f'{variant}.sqlite'
        create_database(databases[variant], case.domain, data[variant])
    return databases, data


class FakeModel:
    model = 'test-only'
    def __init__(self, responses):
        self.responses = iter(responses)
        self.prompts = []
    def complete_with_metadata(self, prompt):
        self.prompts.append(prompt)
        return json.dumps({'action':'REPAIR','sql':next(self.responses)}), {'prompt_tokens':5,'completion_tokens':3}


GOOD = "SELECT COUNT(*) AS closed_tickets FROM tickets WHERE closed_day >= '2026-03-01' AND closed_day < '2026-04-01'"


@pytest.mark.parametrize('method,calls', [('one_shot',1),('execution_retry',1),('checked_agent',2)])
def test_methods_use_same_pipeline_without_runtime_answers(tmp_path, method, calls):
    case = next(c for c in CASES if c.case_id == 'march_closed')
    dbs, data = sources(tmp_path, case)
    model = FakeModel([GOOD]*3)
    result = run_episode(case,0,method,'clean',0,'timeout',dbs,data,model)
    assert result['accepted_correct'] and result['same_query_correct_on_both_instances']
    assert not result['auto_released']
    assert result['source_unchanged'] and len(result['calls']) == calls
    assert result['pipeline']['result']['validation_scope'] == 'contract_checks_not_semantic_correctness'
    assert all('verification_sql' not in prompt and 'expected_cents' not in prompt for prompt in model.prompts)
    if method == 'checked_agent':
        assert GOOD not in model.prompts[1]  # independent checker never receives primary SQL


def test_error_retry_can_fix_where_one_shot_stops(tmp_path):
    case = next(c for c in CASES if c.case_id == 'march_closed')
    dbs, data = sources(tmp_path, case)
    for method, succeeds in [('one_shot',False),('execution_retry',True)]:
        result = run_episode(case,0,method,'clean',0,'timeout',dbs,data,
                             FakeModel(['SELECT missing FROM tickets', GOOD]))
        assert result['accepted_correct'] is succeeds


def test_relational_plan_pipeline_records_both_calls_and_draft(tmp_path):
    from sql_agent.relational_plan import FIELDS
    case = next(c for c in CASES if c.case_id == 'march_closed')
    dbs, data = sources(tmp_path, case)

    class PlanModel(FakeModel):
        def complete_with_metadata(self, prompt):
            if not self.prompts:
                self.prompts.append(prompt)
                return json.dumps({k: 'not applicable' for k in FIELDS}), {
                    'prompt_tokens': 5, 'completion_tokens': 3}
            return super().complete_with_metadata(prompt)

    result = run_episode(case, 0, 'plan_then_sql', 'clean', 0, 'timeout',
                         dbs, data, PlanModel([GOOD]))
    assert result['accepted_correct'] and result['source_unchanged']
    assert not result['auto_released']
    assert len(result['calls']) == 2
    trace = result['pipeline']['result']['agent_trajectory']
    assert sum(t['step'] == 'relational_plan' for t in trace) == 1
    assert sum(t['step'] == 'propose' for t in trace) == 1


@pytest.mark.parametrize('fault', ['timeout','429'])
def test_injected_fault_recorded_and_recovers(tmp_path, monkeypatch, fault):
    monkeypatch.setattr('sql_agent.model_recovery.time.sleep', lambda _: None)
    case = next(c for c in CASES if c.case_id == 'march_closed')
    dbs, data = sources(tmp_path, case)
    result = run_episode(case,0,'checked_agent','transient',0,fault,dbs,data,FakeModel([GOOD]*2))
    assert result['accepted_correct'] and len(result['calls']) == 3
    assert result['calls'][0]['injected'] == fault and result['calls'][0]['usage'] is None


def test_false_rejection_not_hidden_as_success(tmp_path):
    case = next(c for c in CASES if c.case_id == 'march_closed')
    dbs, data = sources(tmp_path, case)
    result = run_episode(case,0,'checked_agent','clean',0,'timeout',dbs,data,
                         FakeModel([GOOD, 'SELECT 999 AS closed_tickets', GOOD]))
    assert result['candidate_correct'] and not result['accepted_correct']
    summary = summarize([result], 1)
    assert summary['correct_candidate_held'] == 1
    assert summary['accepted_candidate_precision'] is None


def test_budget_shared_between_primary_and_checker():
    state = {'calls': [], 'denied': 0, 'fault': None}
    model = FakeModel([GOOD]*CALL_LIMIT)
    a, b = ObservedModel(model,state,'primary'), ObservedModel(model,state,'checker')
    for i in range(CALL_LIMIT):
        (a if i % 2 else b).complete_with_metadata('prompt')
    with pytest.raises(CallBudgetExceeded): b.complete_with_metadata('prompt')
    assert len(state['calls']) == len(model.prompts) == CALL_LIMIT
    assert state['denied'] == 1


def test_cross_instance_check_catches_accidental_constant_match(tmp_path):
    case = next(c for c in CASES if c.case_id == 'march_deliveries')
    dbs, data = sources(tmp_path, case)
    scores = score_sql(case, 'SELECT 1 AS march_parcels', dbs, data)
    assert [s['correct'] for s in scores] == [True,False]


def test_cluster_interval_groups_trials_and_instances_by_question():
    rows = [{'case_id':case, 'variant':v, 'trial':t, 'method':m, 'profile':'clean',
             'accepted_correct':m=='checked_agent'}
            for case in ('a','b') for v in (0,1) for t in range(3)
            for m in ('checked_agent','execution_retry')]
    result = paired_difference(rows,'checked_agent','execution_retry')
    assert result['question_clusters'] == 2
    assert result['mean_difference'] == 1 and result['cluster_bootstrap_95_percentile_interval'] == [1,1]
