"""Offline evaluation primitives. No score here certifies deployment readiness."""
from collections import Counter, defaultdict
import math


def compare_output(actual, expected, *, ordered=True):
    """Missing oracle is unknown; empty expected rows are a valid oracle."""
    if expected is None:
        return None
    if actual is None or list(actual['columns']) != list(expected['columns']):
        return False
    left, right = [tuple(r) for r in actual['rows']], [tuple(r) for r in expected['rows']]
    return left == right if ordered else Counter(left) == Counter(right)


def public_task_audit(tasks):
    ids = [t['instance_id'] for t in tasks]
    missing = [t['instance_id'] for t in tasks if not t.get('sol_sql') or not t.get('test_cases')]
    return {'tasks': len(tasks), 'unique_ids': len(set(ids)), 'missing_oracle_ids': missing,
            'ready_to_score': bool(tasks) and len(ids) == len(set(ids)) and not missing,
            'score': None, 'reason': 'Availability audit only; no task execution or grading.'}


def summarize(episodes, *, repeats):
    if repeats < 1:
        raise ValueError('repeats must be positive')
    groups = defaultdict(list)
    seen = set()
    for e in episodes:
        key = (e['task_id'], e['profile'], e['trial'])
        if key in seen:
            raise ValueError('duplicate trial')
        seen.add(key)
        groups[(e['task_id'], e['profile'])].append(e)
    complete = [v for v in groups.values() if len(v) == repeats and
                {e['trial'] for e in v} == set(range(repeats))]
    def rate(n, d):
        return n / d if d else None
    published = [e for e in episodes if e['released']]
    graded_published = [e for e in published if e['candidate_correct'] is not None]
    calls = [c for e in episodes for c in e['calls']]
    latencies = sorted(e['elapsed_ms'] for e in episodes)
    return {
        'episodes': len(episodes), 'task_profile_groups': len(groups),
        'complete_repeat_groups': len(complete), 'incomplete_repeat_groups': len(groups)-len(complete),
        'pass_all_k': rate(sum(all(e['task_success'] is True for e in v) for v in complete), len(complete)),
        'pass_any_k': rate(sum(any(e['task_success'] is True for e in v) for v in complete), len(complete)),
        'k': repeats, 'task_successes': sum(e['task_success'] is True for e in episodes),
        'candidate_correct': sum(e['candidate_correct'] is True for e in episodes),
        'candidate_incorrect': sum(e['candidate_correct'] is False for e in episodes),
        'candidate_unknown': sum(e['candidate_correct'] is None for e in episodes),
        'db_mutations': sum(not e['db_unchanged'] for e in episodes),
        'released': len(published), 'release_coverage': rate(len(published), len(episodes)),
        'release_correctness': rate(sum(e['candidate_correct'] is True for e in graded_published),len(graded_published)),
        'released_ungraded': len(published)-len(graded_published),
        'handoffs': sum(e['status'] == 'needs_review' for e in episodes),
        'fault_exposed': sum(e['fault_exposed'] for e in episodes),
        'fault_recovered': sum(e['fault_exposed'] and e['task_success'] is True for e in episodes),
        'fault_stopped_without_release': sum(e['fault_exposed'] and e['routing'] == 'STOP' and not e['released'] for e in episodes),
        'model_requests': len(calls), 'injected_calls': sum(bool(c.get('injected')) for c in calls),
        'prompt_tokens_observed': sum(c.get('metadata', {}).get('prompt_tokens', 0) for c in calls),
        'completion_tokens_observed': sum(c.get('metadata', {}).get('completion_tokens', 0) for c in calls),
        'calls_without_usage': sum('metadata' not in c for c in calls),
        'elapsed_ms_total': sum(latencies),
        'pipeline_p95_ms': latencies[math.ceil(.95*len(latencies))-1] if latencies else None,
        'monetary_cost': None, 'monetary_cost_reason': 'No hardware/power price or human review cost supplied.',
    }
