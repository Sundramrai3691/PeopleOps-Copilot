import pytest
from sql_agent.agent_evaluation import compare_output, public_task_audit, summarize


def test_oracle_missing_is_unknown_but_empty_result_is_valid():
    empty = {'columns': ['x'], 'rows': []}
    assert compare_output(empty, None) is None
    assert compare_output(empty, empty) is True
    assert compare_output(None, empty) is False
    assert not public_task_audit([])['ready_to_score']
    assert not public_task_audit([{'instance_id':'x','sol_sql':[], 'test_cases':[]}])['ready_to_score']


def test_bag_comparison_preserves_duplicates_and_order_is_explicit():
    a = {'columns':['x'], 'rows':[[1],[2],[1]]}
    b = {'columns':['x'], 'rows':[[1],[1],[2]]}
    assert not compare_output(a,b)
    assert compare_output(a,b,ordered=False)
    assert not compare_output(a,{'columns':['x'], 'rows':[[1],[2]]},ordered=False)


def episode(trial, success):
    return dict(task_id='x', profile='normal', trial=trial, task_success=success,
                candidate_correct=success, released=False, db_unchanged=True,
                calls=[],elapsed_ms=1,status='needs_review',routing='KEEP',fault_exposed=False)


def test_repeat_metric_not_retry_metric_and_zero_release_not_perfect():
    rows=[episode(0,True),episode(1,False),episode(2,True)]
    s=summarize(rows,repeats=3)
    assert s['pass_all_k']==0 and s['pass_any_k']==1
    assert s['release_correctness'] is None
    assert summarize(rows[:2],repeats=3)['pass_all_k'] is None
    with pytest.raises(ValueError): summarize(rows+rows[:1], repeats=3)
