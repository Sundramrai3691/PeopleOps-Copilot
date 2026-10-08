from scripts.evaluation.holdout_metrics import metrics


def row(i, *, correct=False, candidate=True, verifier='disagreement', status='needs_review', usage=True):
    return {'case_id':str(i),'family':'family'+str(i),'correct':correct,'has_candidate':candidate,
            'verifier_status':verifier,'status':status,'seconds':float(i+1),
            'telemetry':{'usage_complete':usage,'prompt_tokens_observed':10,'completion_tokens_observed':5,'calls':1}}


def test_wrong_agreement_is_visible_even_when_human_gate_blocks_release():
    result=metrics([row(0,correct=True,verifier='agreement'),row(1,verifier='agreement'),row(2),row(3,candidate=False,status='failed',verifier='unavailable')],resamples=100)
    assert result['task_accuracy']['rate']==.25
    assert result['verifier_false_accept_rate']=={'numerator':1,'denominator':2,'rate':.5}
    assert result['verifier_false_discovery_rate']['rate']==.5
    assert result['system_false_accept_rate']['rate']==0
    assert result['system_false_discovery_rate']['rate'] is None
    assert result['review']['rate']==.75
    assert result['token_cost']['tokens_per_correct_result']==60
    assert result['p95_seconds']==4


def test_unknown_cost_and_undefined_no_success_denominators():
    result=metrics([row(0,candidate=False,status='failed',usage=False)],resamples=100)
    assert result['token_cost']['tokens_per_correct_result'] is None
    assert result['verifier_false_accept_rate']['rate'] is None
    assert result['verifier_false_discovery_rate']['rate'] is None


def test_wrong_system_release_counts_even_without_verifier_agreement():
    result=metrics([row(0,status='completed')],resamples=100)
    assert result['system_false_accept_rate']['rate']==1
    assert result['verifier_false_accept_rate']['rate']==0
