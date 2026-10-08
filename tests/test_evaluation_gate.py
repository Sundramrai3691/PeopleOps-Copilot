from sql_agent.evaluation_gate import assess
import pytest


def report():
    return {'schema_version':1,'configuration_id':'a'*64,'dataset_id':'b'*64,'model_profile_id':'c'*64,
            'split':'new_templates','strata':{name:{'planned':50,'observed':50,'correct':25,
                'incorrect':25,'unavailable':0,'correct_accepted':25,'incorrect_accepted':0}
                for name in ('generated','challenge')}}


def test_false_acceptance_blocks_general_query_release():
    data=report();data['strata']['generated']['incorrect_accepted']=1
    result=assess(data,expected_configuration='a'*64)
    assert result['passed'] is False
    assert 'generated:incorrect_answers_accepted' in result['reasons']


@pytest.mark.parametrize('change,reason',[
    ({'observed':49,'correct':24,'correct_accepted':24},'incomplete'),
    ({'correct_accepted':20},'correct_pass_below_90_percent'),
    ({'correct':50,'incorrect':0,'correct_accepted':50},'insufficient_evidence'),
    ({'correct_accepted':26},'inconsistent_counts'),
    ({'planned':True},'invalid_counts'),
])
def test_incomplete_or_invalid_evidence_cannot_pass(change,reason):
    data=report();data['strata']['generated'].update(change)
    result=assess(data,expected_configuration='a'*64)
    assert not result['passed']
    assert 'generated:'+reason in result['reasons']


def test_stale_configuration_and_old_development_set_block_release():
    data=report();data['split']='dev'
    result=assess(data,expected_configuration='d'*64)
    assert not result['passed']
    assert {'configuration_mismatch','new_template_evidence_required'}<=set(result['reasons'])


def test_changed_model_profile_cannot_reuse_old_scores():
    data=report();data['model_profile_id']='c'*64
    result=assess(data,expected_configuration='a'*64,expected_model_profile='d'*64)
    assert not result['passed']
    assert 'model_profile_mismatch' in result['reasons']


def test_complete_matching_evidence_passes_only_offline_gate():
    result=assess(report(),expected_configuration='a'*64,expected_model_profile='c'*64)
    assert result['passed']
    assert 'Never authorizes' in result['scope']
