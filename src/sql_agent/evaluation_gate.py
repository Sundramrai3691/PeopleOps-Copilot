"""Conservative offline release decision, not runtime authorization or a scorer.

Input counts must come from separately audited evaluation. This gate cannot
authenticate the measurements or establish that a dataset is representative.
"""
import re


def assess(report, *, expected_configuration, expected_model_profile=None):
    reasons=[]
    if not isinstance(report,dict):report={}
    if report.get('schema_version')!=1:reasons.append('invalid_schema_version')
    for key in ('configuration_id','dataset_id','model_profile_id'):
        if not isinstance(report.get(key),str) or not re.fullmatch('[0-9a-f]{64}',report[key]):
            reasons.append('invalid_'+key)
    if report.get('configuration_id')!=expected_configuration:reasons.append('configuration_mismatch')
    if not isinstance(expected_model_profile,str) or not re.fullmatch('[0-9a-f]{64}',expected_model_profile):
        reasons.append('expected_model_profile_required')
    if report.get('model_profile_id')!=expected_model_profile:reasons.append('model_profile_mismatch')
    if report.get('split')!='new_templates':reasons.append('new_template_evidence_required')
    strata=report.get('strata')
    if not isinstance(strata,dict) or set(strata)!={'generated','challenge'}:
        reasons.append('both_strata_required');strata={}
    for name,data in strata.items():
        keys=('planned','observed','correct','incorrect','unavailable','correct_accepted','incorrect_accepted')
        if not isinstance(data,dict) or any(type(data.get(k)) is not int or data[k]<0 for k in keys):
            reasons.append(name+':invalid_counts');continue
        if (data['observed']>data['planned'] or data['observed']!=data['correct']+data['incorrect']+data['unavailable']
                or data['correct_accepted']>data['correct'] or data['incorrect_accepted']>data['incorrect']):
            reasons.append(name+':inconsistent_counts');continue
        if data['planned']==0 or data['observed']!=data['planned']:reasons.append(name+':incomplete')
        if data['unavailable']:reasons.append(name+':candidate_failures')
        if data['correct']<10 or data['incorrect']<25:reasons.append(name+':insufficient_evidence')
        if data['correct_accepted']*10<data['correct']*9:reasons.append(name+':correct_pass_below_90_percent')
        if data['incorrect_accepted']:reasons.append(name+':incorrect_answers_accepted')
    return {'gate_version':'general-query-v1','passed':not reasons,'reasons':reasons,
            'scope':'Offline eligibility only. Never authorizes publication or a database action.',
            'requirements':{'min_correct_per_stratum':10,'min_incorrect_per_stratum':25,
                            'min_correct_pass_rate':0.9,'allowed_incorrect_accepts':0,
                            'complete_required':True,'candidate_failures_allowed':0}}
