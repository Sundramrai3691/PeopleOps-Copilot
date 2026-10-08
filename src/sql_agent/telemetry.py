"""Per-job model usage accounting, including failed and retried requests."""

from copy import deepcopy


def token_cost(summary):
    """Observed token consumption, not a guessed monetary charge."""
    complete = summary['usage_complete']
    observed = summary['prompt_tokens_observed'] + summary['completion_tokens_observed']
    return {'input_tokens': summary['prompt_tokens_observed'] if complete else None,
            'output_tokens': summary['completion_tokens_observed'] if complete else None,
            'total_tokens': observed if complete else None,
            'observed_total_tokens': observed, 'complete': complete, 'unit': 'tokens'}


def combine_attempts(previous, current, *, expected_previous):
    """Sum disjoint worker-attempt snapshots; never count cumulative snapshots twice."""
    result = deepcopy(current)
    for old in previous:
        for key in ('calls', 'failed_calls', 'retries', 'prompt_tokens_observed',
                    'completion_tokens_observed', 'calls_without_complete_usage', 'pipeline_elapsed_ms'):
            result[key] += old[key]
        for name, stage in old['stages'].items():
            target = result['stages'].setdefault(name, {})
            for key, value in stage.items():
                target[key] = target.get(key, 0) + value
    missing = max(0, expected_previous-len(previous))
    result.update(accounting_scope='observed_job_attempts', unobserved_job_attempts=missing,
                  usage_complete=not missing and all(x['usage_complete'] for x in [*previous,current]))
    result['token_cost'] = token_cost(result)
    return result


def summarize_calls(recovery, elapsed_ms):
    stages = {}
    for stage, events in recovery.items():
        calls = [e for e in events if e['event'] in ('success', 'failure')]
        stages[stage] = {
            'calls': len(calls),
            'failed_calls': sum(e['event'] == 'failure' for e in calls),
            'retries': sum(e['attempt'] > 1 for e in calls),
            'call_elapsed_ms': round(sum(e.get('elapsed_ms', 0) for e in calls), 3),
            'retry_wait_ms': round(sum(e.get('delay_seconds', 0) * 1000 for e in calls), 3),
            'prompt_tokens_observed': sum(e.get('prompt_tokens') or 0 for e in calls),
            'completion_tokens_observed': sum(e.get('completion_tokens') or 0 for e in calls),
            'calls_without_complete_usage': sum(e.get('prompt_tokens') is None or
                                                e.get('completion_tokens') is None for e in calls),
            **{name+'_observed':round(sum(e.get(name) or 0 for e in calls),3)
               for name in ('model_load_ms','prompt_eval_ms','generation_ms')},
            'calls_without_provider_timing':sum(any(e.get(name) is None for name in
                ('model_load_ms','prompt_eval_ms','generation_ms')) for e in calls),
        }
    totals = {key: sum(stage[key] for stage in stages.values()) for key in (
        'calls', 'failed_calls', 'retries', 'prompt_tokens_observed',
        'completion_tokens_observed', 'calls_without_complete_usage')}
    summary = {
        'pipeline_elapsed_ms': round(elapsed_ms, 3),
        'latency_scope': 'worker_pipeline_excluding_queue_wait_and_http',
        'stages': stages, **totals,
        'usage_complete': totals['calls_without_complete_usage'] == 0,
        'monetary_cost': None,
        'cost_reason': 'Cost is measured in input and output tokens; no model unit prices supplied for monetary conversion.',
    }
    summary['token_cost'] = token_cost(summary)
    return summary
