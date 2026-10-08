"""Frozen, paired SQL pipeline comparison. No runtime reference SQL or fallback."""
import argparse
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import shutil
import tempfile
import time
import urllib.error
import urllib.request

from sql_agent.agent_evaluation import compare_output
from sql_agent.bounded_runtime import ActionProposal
from sql_agent.data_agent import SQLAgentPlanner, SQLAgentSession
from sql_agent.jobs import SqliteJobRepository
from sql_agent.paired_benchmark import CASES, SCHEMAS, create_database, expected, fixture
from sql_agent.pipeline import JobPipeline
from sql_agent.planner import OllamaPlannerModel
from sql_agent.native_sql import NativeSQLPlanner
from sql_agent.semantic_review import IndependentSQLPlanner
from sql_agent.sql_config import SQLTaskRegistry

ROOT = Path(__file__).resolve().parents[2]
METHODS = ('one_shot', 'execution_retry', 'checked_agent')
CALL_LIMIT = 9


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False))


class CallBudgetExceeded(RuntimeError):
    pass


class ObservedModel:
    """Shared total attempt budget across primary, checker and transport retries."""
    def __init__(self, delegate, state, role):
        self.delegate, self.state, self.role = delegate, state, role
        self.model = delegate.model

    def complete_with_metadata(self, prompt):
        if len(self.state['calls']) >= CALL_LIMIT:
            self.state['denied'] += 1
            raise CallBudgetExceeded('shared model attempt budget exhausted')
        record = {'role': self.role, 'prompt_sha256': sha_bytes(prompt.encode()),
                  'injected': None, 'usage': None}
        self.state['calls'].append(record)
        started = time.perf_counter()
        try:
            if len(self.state['calls']) == 1 and self.state['fault']:
                record['injected'] = self.state['fault']
                if self.state['fault'] == 'timeout':
                    raise TimeoutError('Controlled benchmark injection; no network wait')
                raise urllib.error.HTTPError('evaluation://injected', 429, 'injected rate limit', {'Retry-After': '0'}, None)
            text, usage = self.delegate.complete_with_metadata(prompt)
            record.update(response=text, usage=usage)
            return text, usage
        except Exception as exc:
            record['error_type'] = type(exc).__name__
            raise
        finally:
            record['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 3)


def score_sql(case, sql, databases, data):
    """Reuse a frozen generated query across BOTH instances, offline only."""
    scores = []
    for variant in range(2):
        import sqlite3
        connection = sqlite3.connect(databases[variant].as_uri() + '?mode=ro', uri=True)
        try:
            session = SQLAgentSession(connection, case.task(databases[variant]).contract)
            proposal = ActionProposal('REPAIR', 'sql_query', {'sql': sql})
            allowed, reason = session.authorize(proposal)
            if not allowed:
                scores.append({'correct': False, 'error': reason})
                continue
            output = session.execute(proposal)
            scores.append({'correct': compare_output(output, expected(case, data[variant])), 'output': output})
        except Exception as exc:
            scores.append({'correct': False, 'error': type(exc).__name__})
        finally:
            connection.close()
    return scores


def run_episode(case, variant, method, profile, trial, fault, databases, data, delegate):
    state = {'calls': [], 'denied': 0, 'fault': fault if profile == 'transient' else None}
    primary = SQLAgentPlanner(ObservedModel(delegate, state, 'primary'),
                                 relational_plan=method == 'plan_then_sql', data_probe=method == 'probe_then_sql')
    if method in ('native_sql', 'native_sql_guided', 'native_sql_repair'):
        primary = NativeSQLPlanner(ObservedModel(delegate, state, 'primary'),
                                   semantic_guidance=method in ('native_sql_guided', 'native_sql_repair'))
    # A callable without .model deliberately disables automatic independent review.
    def plain(context):
        proposal = primary(context)
        plain.last_plan = primary.last_plan
        return proposal
    plain.recovery_events = primary.recovery_events
    plain.data_probe = primary.data_probe
    if primary.data_probe:
        plain.request_probes = primary.request_probes
    reviewer = IndependentSQLPlanner(ObservedModel(delegate, state, 'checker')) if method == 'checked_agent' else None
    with tempfile.TemporaryDirectory(prefix='paired-sql-') as temp:
        source = Path(temp) / 'source.sqlite'
        shutil.copyfile(databases[variant], source)
        before = sha_bytes(source.read_bytes())
        task = case.task(source)
        repository = SqliteJobRepository(Path(temp) / 'jobs.sqlite')
        job, _ = repository.submit('sql_analysis', {'task_id': case.case_id}, 'episode')
        pipeline = JobPipeline(SQLTaskRegistry((task,)), plain,
                               max_attempts=1 if method in ('one_shot', 'plan_then_sql', 'probe_then_sql', 'native_sql', 'native_sql_guided') else 3, reviewer=reviewer)
        started = time.perf_counter()
        outcome = pipeline.run(job)
        elapsed_ms = (time.perf_counter() - started) * 1000
        unchanged = before == sha_bytes(source.read_bytes())
    result = outcome.result
    # Independent expected values enter evaluation only AFTER pipeline completion.
    candidate = result['output'] or result['candidate_output']
    correct = compare_output(candidate, expected(case, data[variant])) if candidate is not None else None
    accepted = result['routing']['decision'] == 'KEEP'
    proposals = [t['proposal'] for t in result['agent_trajectory'] if t['step'] == 'propose'
                 and t['proposal'].get('tool') == 'sql_query']
    first = score_sql(case, proposals[0]['arguments']['sql'], databases, data) if proposals else None
    final = score_sql(case, proposals[-1]['arguments']['sql'], databases, data) if proposals else None
    # Do not mistake an earlier held candidate for the final proposed query.
    cross_correct = accepted and final is not None and all(s['correct'] for s in final)
    return {
        'case_id': case.case_id, 'domain': case.domain, 'variant': variant,
        'method': method, 'profile': profile, 'trial': trial,
        'fault_type': state['fault'], 'source_unchanged': unchanged,
        'controller_accepted': accepted, 'candidate_correct': correct,
        'accepted_correct': accepted and correct is True and unchanged,
        'accepted_incorrect': accepted and correct is False,
        'first_query_correct': first[variant]['correct'] if first else None,
        'same_query_correct_on_both_instances': bool(cross_correct),
        'first_query_test_suite': first, 'final_query_test_suite': final,
        'auto_released': result['release']['approved'],
        'elapsed_ms': round(elapsed_ms, 3), 'calls': state['calls'],
        'budget_denials': state['denied'], 'pipeline': asdict(outcome),
    }


def summarize(rows, repeats):
    groups = defaultdict(list)
    for row in rows:
        groups[(row['case_id'], row['variant'], row['profile'])].append(row)
    complete = [v for v in groups.values() if len(v) == repeats and
                {r['trial'] for r in v} == set(range(repeats))]
    calls = [call for row in rows for call in row['calls']]
    times = sorted(row['elapsed_ms'] for row in rows)
    n = len(rows)
    accepted = sum(row['controller_accepted'] for row in rows)
    success = sum(row['accepted_correct'] for row in rows)
    observed = lambda key: sum((c.get('usage') or {}).get(key) or 0 for c in calls)
    return {
        'episodes': n, 'accepted_correct': success,
        'accepted_incorrect': sum(row['accepted_incorrect'] for row in rows),
        'held_or_stopped': n - accepted,
        'candidate_correct_before_controller_decision': sum(row['candidate_correct'] is True for row in rows),
        'correct_candidate_held': sum(row['candidate_correct'] is True and not row['controller_accepted'] for row in rows),
        'accepted_correct_rate': success / n if n else None,
        'controller_acceptance_rate': accepted / n if n else None,
        'accepted_candidate_precision': success / accepted if accepted else None,
        'first_query_correct': sum(row['first_query_correct'] is True for row in rows),
        'initial_wrong_to_accepted_correct': sum(row['first_query_correct'] is False and row['accepted_correct'] for row in rows),
        'initial_correct_not_retained_correct': sum(row['first_query_correct'] is True and not row['accepted_correct'] for row in rows),
        'same_query_correct_on_both_instances': sum(row['same_query_correct_on_both_instances'] for row in rows),
        'complete_repeat_groups': len(complete),
        'observed_all_repeats_success_rate': sum(all(r['accepted_correct'] for r in group) for group in complete) / len(complete) if complete else None,
        'observed_any_repeat_success_rate': sum(any(r['accepted_correct'] for r in group) for group in complete) / len(complete) if complete else None,
        'source_mutations': sum(not row['source_unchanged'] for row in rows),
        'automatic_releases': sum(row['auto_released'] for row in rows),
        'model_attempts_including_injected_faults': len(calls),
        'real_provider_requests': sum(c['injected'] is None for c in calls),
        'injected_faults': sum(c['injected'] is not None for c in calls),
        'budget_denials': sum(row['budget_denials'] for row in rows),
        'prompt_tokens_observed': observed('prompt_tokens'),
        'completion_tokens_observed': observed('completion_tokens'),
        'calls_without_complete_usage': sum(any((c.get('usage') or {}).get(k) is None for k in ('prompt_tokens','completion_tokens')) for c in calls),
        'attempts_per_accepted_correct_including_failed_episodes': len(calls) / success if success else None,
        'pipeline_p50_ms': times[math.ceil(.50*n)-1] if n else None,
        'pipeline_p95_ms': times[math.ceil(.95*n)-1] if n else None,
        'monetary_cost': None,
    }


def paired_difference(rows, left, right):
    """Resample QUESTION families, not correlated variants/trials as independent."""
    clusters = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if row['profile'] == 'clean' and row['method'] in (left, right):
            clusters[row['case_id']][row['method']].append(int(row['accepted_correct']))
    differences = [sum(v[left])/len(v[left]) - sum(v[right])/len(v[right])
                   for v in clusters.values() if left in v and right in v]
    if not differences:
        return None
    rng = random.Random(89311)
    samples = sorted(sum(rng.choices(differences, k=len(differences)))/len(differences) for _ in range(2000))
    return {'left': left, 'right': right, 'profile': 'clean', 'question_clusters': len(differences),
            'mean_difference': sum(differences)/len(differences),
            'cluster_bootstrap_95_percentile_interval': [samples[49], samples[1949]],
            'scope': 'Descriptive uncertainty on this small synthetic task-family sample; not a population guarantee.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--model', default='qwen3:8b')
    parser.add_argument('--suite', choices=('development', 'billing'), default='development')
    parser.add_argument('--max-tokens', type=int, default=256)
    parser.add_argument('--methods', nargs='+', choices=(*METHODS, 'plan_then_sql', 'probe_then_sql', 'native_sql', 'native_sql_guided', 'native_sql_repair'), default=list(METHODS))
    parser.add_argument('--profiles', nargs='+', choices=('clean', 'transient'), default=['clean', 'transient'])
    args = parser.parse_args()
    if len(set(args.methods)) != len(args.methods) or len(set(args.profiles)) != len(args.profiles):
        parser.error('duplicate methods/profiles')
    methods = tuple(args.methods)
    native = all(m.startswith('native_sql') for m in methods)
    if any(m.startswith('native_sql') for m in methods) and not native:
        parser.error('native SQL protocol must be evaluated in a separate run')
    if args.repeats < 2:
        parser.error('at least two full trials required')
    extra_sources = []
    if args.suite == 'billing':
        global CASES, SCHEMAS, fixture, create_database, expected
        from scripts.evaluation import transfer_sql_cases as billing
        CASES, SCHEMAS, expected = billing.CASES, {'billing': billing.SCHEMA}, billing.expected
        fixture = lambda domain, variant: billing.fixture(variant)
        create_database = lambda path, domain, data: billing.create_database(path, data)
        extra_sources = [Path(billing.__file__).resolve()]
    args.output.mkdir(parents=True, exist_ok=False)
    sources = list((ROOT/'src/sql_agent').glob('*.py')) + [Path(__file__).resolve()] + extra_sources
    hashes = {str(p.relative_to(ROOT)): sha_bytes(p.read_bytes()) for p in sources}
    snapshot = args.output/'source_snapshot'
    for path in sources:
        destination = snapshot/path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    with urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=5) as response:
        models = [m for m in json.load(response)['models'] if m['name'] == args.model]
    if not models:
        raise RuntimeError('requested model unavailable')
    datasets, databases = {}, {}
    for domain in SCHEMAS:
        datasets[domain], databases[domain] = {}, {}
        for variant in range(2):
            data = fixture(domain, variant)
            path = (args.output/f'{domain}_{variant}.sqlite').resolve()
            create_database(path, domain, data)
            datasets[domain][variant], databases[domain][variant] = data, path
            write_json(args.output/f'{domain}_{variant}_input.json', data)
    schedule = [(case, variant, profile, trial) for trial in range(args.repeats)
                for case in CASES for variant in range(2) for profile in args.profiles]
    rng = random.Random(100719)
    rng.shuffle(schedule)
    plan = []
    for index, (case, variant, profile, trial) in enumerate(schedule):
        offset = index % len(methods)
        rotated = methods[offset:] + methods[:offset]
        fault = 'timeout' if list(CASES).index(case) % 2 == 0 else '429'
        for method in rotated:
            plan.append((case, variant, method, profile, trial, fault))
    manifest = {'created_before_inference': datetime.now(timezone.utc).isoformat(),
                'source_sha256': hashes, 'model': models, 'model_options': {'temperature': 0, 'num_predict': args.max_tokens, 'json_mode': not native, 'think': False, 'timeout_seconds': 30},
                'cases': [asdict(c) for c in CASES], 'episodes_planned': len(plan),
                'database_sha256': {p.name: sha_bytes(p.read_bytes()) for d in databases.values() for p in d.values()},
                'repeats': args.repeats, 'shared_model_attempt_limit': CALL_LIMIT,
                'selected_methods': methods, 'profiles': args.profiles,
                'suite': args.suite, 'methods': {'native_sql_repair': 'guided native SQL with up to three execution-feedback rounds, no checker','native_sql_guided': 'one SQL-text generation with generic grain/alias/condition checklist; no checker',
                            'native_sql': 'one SQL-text generation with contract aliases; no checker',
                            'probe_then_sql': 'one probe-selection call, at most two bounded reads, one SQL generation; no checker',
                            'plan_then_sql': 'one relational-plan call then one SQL generation; no checker',
                            'one_shot': 'one SQL planning round, no independent checker',
                            'execution_retry': 'up to three SQL rounds on execution/structural errors, no checker',
                            'checked_agent': 'up to three SQL rounds plus one cached independent checker'},
                'controls': 'Same model, input schema/question, structural contracts, SQL policy, transport retry helper and total attempt ceiling. One-shot intentionally has fewer planning rounds; realized usage is reported.',
                'faults': 'One simulated first-primary-call timeout or 429 per transient episode. Alternating task families, identical fault across methods. Injected failures count toward the shared budget.',
                'scope': 'Development regression on previously inspected synthetic tasks; not an unseen evaluation or external benchmark. No inference-based tuning during this run.',
                'success': 'Independent Python-correct candidate retained by controller, with unchanged source. Generic API release stays disabled for all methods; retained correctness is NOT user-delivered task success.',
                'runtime_oracle': 'No reference SQL, expected output, or catalog fallback supplied to production pipeline or checker.',
                'ordering': 'Seeded shuffled paired groups with rotating method order; sequential inference; live demo may share machine.',
                'schedule': [[c.case_id,v,m,p,t,f] for c,v,m,p,t,f in plan]}
    write_json(args.output/'manifest.json', manifest)
    rows = []
    model = OllamaPlannerModel('http://127.0.0.1:11434', args.model, timeout=30,
                               max_tokens=args.max_tokens, json_mode=not native)
    for i, (case, variant, method, profile, trial, fault) in enumerate(plan):
        row = run_episode(case, variant, method, profile, trial, fault,
                          databases[case.domain], datasets[case.domain], model)
        rows.append(row)
        write_json(args.output/f'episode_{i:04d}.json', row)
        print(json.dumps({'done': i+1, 'planned': len(plan), 'case': case.case_id,
                          'method': method, 'profile': profile,
                          'accepted_correct': row['accepted_correct'],
                          'calls': len(row['calls'])}), flush=True)
    by_method = {}
    for method in methods:
        group = [r for r in rows if r['method'] == method]
        by_method[method] = {'all': summarize(group, args.repeats),
                             **{p: summarize([r for r in group if r['profile'] == p], args.repeats)
                                for p in ('clean','transient')},
                             'by_fault_type': {f: summarize([r for r in group if r['fault_type'] == f], args.repeats)
                                               for f in ('timeout','429')},
                             'clean_by_domain': {d: summarize([r for r in group if r['profile'] == 'clean' and r['domain'] == d], args.repeats)
                                                 for d in SCHEMAS}}
    result = {'episodes': len(rows), 'distinct_questions': len(CASES), 'database_instances': len(SCHEMAS)*2,
              'question_database_pairs': len(CASES)*2, 'repeats': args.repeats,
              'by_method': by_method,
              'paired_clean_differences': [paired_difference(rows, 'probe_then_sql', 'one_shot'),
                                           paired_difference(rows, 'plan_then_sql', 'one_shot'),
                                           paired_difference(rows, 'checked_agent', 'execution_retry'),
                                           paired_difference(rows, 'execution_retry', 'one_shot')],
              'sources_unchanged': all(sha_bytes((ROOT/p).read_bytes()) == h for p,h in hashes.items()),
              'databases_unchanged': all(sha_bytes((args.output/p).read_bytes()) == h for p,h in manifest['database_sha256'].items()),
              'all_within_attempt_budget': all(len(r['calls']) <= CALL_LIMIT for r in rows),
              'completion': 'All planned episodes recorded, including wrong and stopped cases; no success threshold was invented.'}
    write_json(args.output/'summary.json', result)
    print(json.dumps({'complete': len(rows), 'sources_unchanged': result['sources_unchanged']}), flush=True)
    assert result['sources_unchanged'] and result['databases_unchanged'] and result['all_within_attempt_budget']


if __name__ == '__main__':
    main()
