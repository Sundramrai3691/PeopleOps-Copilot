"""Score-report boundary: distinguish agreement, correctness and publication."""
import pytest
from sql_agent.acceptance_report import summarize, paired_comparison


def test_report_does_not_count_human_review_as_correct_auto_acceptance():
    rows = [
        {'case_id':'a','family':'sum','correct':True,'status':'needs_review','verifier_status':'agreement','seconds':1},
        {'case_id':'b','family':'sum','correct':False,'status':'needs_review','verifier_status':'agreement','seconds':2},
        {'case_id':'c','family':'join','correct':False,'status':'needs_review','verifier_status':'disagreement','seconds':3},
        {'case_id':'d','family':'join','correct':False,'status':'failed','verifier_status':'not_run','seconds':4},
    ]
    report = summarize(rows)
    assert report['correct'] == 1
    assert report['candidate_accuracy'] == .25
    assert report['human_review_rate'] == .75
    assert report['automatic_release_rate'] == 0
    assert report['false_discovery_among_releases'] is None
    assert report['incorrect_answer_detection_recall'] == .5
    assert report['incorrect_agreement_rate'] == .5
    assert report['unavailable'] == 1


def test_paired_report_matches_ids_and_keeps_regressions():
    baseline = [{'case_id':str(i),'family':str(i//2),'correct':c} for i,c in enumerate([True,False,True,False])]
    candidate = [{'case_id':str(i),'family':str(i//2),'correct':c} for i,c in enumerate([True,True,False,True])]
    result = paired_comparison(baseline, list(reversed(candidate)))
    assert result['fixed'] == 2
    assert result['regressed'] == 1
    assert result['accuracy_delta'] == .25
    assert result['clusters'] == 2
    assert result['mcnemar_exact_p_iid'] == 1
    with pytest.raises(ValueError, match='paired'):
        paired_comparison(baseline, candidate[:-1])


def test_token_cost_per_correct_result_includes_spend_on_failures():
    from sql_agent.telemetry import summarize_calls
    def usage(n):
        return summarize_calls({'planner':[{'event':'success','attempt':1,
            'prompt_tokens':n,'completion_tokens':2}]}, 1)
    rows = [
        {'case_id':'ok','family':'sum','correct':True,'status':'needs_review',
         'verifier_status':'agreement','seconds':1,'telemetry':usage(10)},
        {'case_id':'failed','family':'sum','correct':False,'status':'failed',
         'verifier_status':'not_run','seconds':1,'telemetry':usage(20)}]
    cost = summarize(rows)['token_cost']
    assert cost['total_tokens'] == 34
    assert cost['tokens_per_submitted_job'] == 17
    assert cost['tokens_per_correct_result'] == 34
    rows[1]['telemetry']['usage_complete'] = False
    cost = summarize(rows)['token_cost']
    assert cost['total_tokens'] is None
    assert cost['tokens_per_correct_result'] is None
    assert cost['observed_total_tokens'] == 34
