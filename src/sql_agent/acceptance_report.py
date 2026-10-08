"""Descriptive evaluation of frozen run records; never runtime authorization."""
from collections import defaultdict
import math
import random


def ratio(a, b):
    return a / b if b else None


def wilson(correct, total):
    if not total:
        return None
    z = 1.96
    p = correct / total
    denominator = 1 + z*z/total
    center = (p + z*z/(2*total)) / denominator
    width = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / denominator
    return [center-width, center+width]


def summarize(rows):
    if not rows or len({r['case_id'] for r in rows}) != len(rows):
        raise ValueError('nonempty unique evaluation IDs required')
    candidates = [r for r in rows if r['status'] in ('needs_review','completed','review_approved')]
    wrong = [r for r in candidates if not r['correct']]
    correct = [r for r in candidates if r['correct']]
    released = [r for r in rows if r['status'] == 'completed']
    times = sorted(r['seconds'] for r in rows)
    usage = [r.get('telemetry', {}) for r in rows]
    observed_input = sum(u.get('prompt_tokens_observed',0) for u in usage)
    observed_output = sum(u.get('completion_tokens_observed',0) for u in usage)
    complete = all(u and u.get('usage_complete') for u in usage)
    total = observed_input + observed_output
    cost = {'unit':'tokens', 'complete':complete,
            'input_tokens':observed_input if complete else None,
            'output_tokens':observed_output if complete else None,
            'total_tokens':total if complete else None, 'observed_total_tokens':total,
            'tokens_per_submitted_job':total/len(rows) if complete else None,
            'tokens_per_correct_result':ratio(total,sum(r['correct'] for r in rows)) if complete else None,
            'scope':'All submitted jobs, including incorrect answers, failures and recorded retries. Missing usage is unknown, not zero.'}
    return {'n':len(rows), 'correct':sum(r['correct'] for r in rows),
        'token_cost':cost,
        'candidate_accuracy':sum(r['correct'] for r in rows)/len(rows),
        'accuracy_wilson95_descriptive_iid':wilson(sum(r['correct'] for r in rows),len(rows)),
        'unavailable':len(rows)-len(candidates),
        'human_review_rate':sum(r['status']=='needs_review' for r in rows)/len(rows),
        'automatic_release_rate':len(released)/len(rows),
        'false_discovery_among_releases':ratio(sum(not r['correct'] for r in released),len(released)),
        'incorrect_answer_detection_recall':ratio(sum(r['verifier_status']=='disagreement' for r in wrong),len(wrong)),
        'incorrect_agreement_rate':ratio(sum(r['verifier_status']=='agreement' for r in wrong),len(wrong)),
        'correct_answer_agreement_rate':ratio(sum(r['verifier_status']=='agreement' for r in correct),len(correct)),
        'p50_seconds':times[math.ceil(.5*len(times))-1],
        'p95_seconds':times[math.ceil(.95*len(times))-1],
        'prompt_tokens_observed':sum(u.get('prompt_tokens_observed',0) for u in usage),
        'completion_tokens_observed':sum(u.get('completion_tokens_observed',0) for u in usage),
        'model_calls_observed':sum(u.get('calls',0) for u in usage),
        'jobs_without_complete_telemetry':sum(not u or not u.get('usage_complete') for u in usage),
        'caution':'Agreement is advisory, not publication or proof. Repeated template instances are clustered, not independent questions.'}


def paired_comparison(baseline, candidate, *, seed=42, resamples=10000):
    a = {r['case_id']:r for r in baseline}
    b = {r['case_id']:r for r in candidate}
    if not a or a.keys()!=b.keys() or len(a)!=len(baseline) or len(b)!=len(candidate):
        raise ValueError('complete unique paired IDs required')
    if not 100 <= resamples <= 100000:
        raise ValueError('invalid resampling budget')
    groups = defaultdict(list)
    fixed = regressed = 0
    for ident in sorted(a):
        if a[ident]['family'] != b[ident]['family']:
            raise ValueError('paired cluster labels differ')
        before, after = bool(a[ident]['correct']), bool(b[ident]['correct'])
        fixed += not before and after
        regressed += before and not after
        groups[a[ident]['family']].append(int(after)-int(before))
    clusters = list(groups.values())
    rng = random.Random(seed)
    draws = []
    for _ in range(resamples):
        sample = [rng.choice(clusters) for _ in clusters]
        draws.append(sum(sum(g) for g in sample)/sum(len(g) for g in sample))
    draws.sort()
    discordant = fixed+regressed
    p = min(1., 2*sum(math.comb(discordant,k) for k in range(min(fixed,regressed)+1))/2**discordant)
    return {'n':len(a),'fixed':fixed,'regressed':regressed,
        'accuracy_delta':(fixed-regressed)/len(a), 'clusters':len(clusters),
        'cluster_bootstrap95':[draws[math.floor(.025*resamples)],draws[math.ceil(.975*resamples)-1]],
        'bootstrap_seed':seed,'bootstrap_resamples':resamples,'mcnemar_exact_p_iid':p,
        'caution':'Cluster bootstrap resamples whole declared families. Few clusters imply weak precision; IID McNemar is only descriptive when cases are dependent.'}
