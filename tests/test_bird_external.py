from types import SimpleNamespace
from scripts.evaluation.validate_bird_external import score, select_cases, DOMAINS


def test_external_grading_reports_duplicate_difference():
    outcome = SimpleNamespace(decision='KEEP', output={'rows': ((1,), (1,))})
    result = score(outcome, ((1,),))
    assert result['execution_match']  # official BIRD-style set semantics
    assert not result['bag_match']    # separate, stricter production signal


def test_stopped_empty_output_is_not_a_correct_empty_answer():
    outcome = SimpleNamespace(decision='STOP', output=None)
    assert not score(outcome, ())['execution_match']


def test_case_selection_is_order_independent_and_ignores_answers():
    rows = [{'question_id': i, 'db_id': domain, 'SQL': 'secret'}
            for domain in DOMAINS for i in range(20)]
    first = [(r['db_id'], r['question_id']) for r in select_cases(rows)]
    changed = [{**r, 'SQL': 'different secret'} for r in reversed(rows)]
    assert first == [(r['db_id'], r['question_id']) for r in select_cases(changed)]
    assert len(first) == 30 and len(set(first)) == 30


def test_all_domains_keeps_every_case_and_excludes_other_databases():
    rows = [{'question_id': f'{domain}:{i}', 'db_id': domain}
            for domain, n in zip(DOMAINS, (30, 32, 48)) for i in range(n)]
    rows.append({'question_id': 'other', 'db_id': 'other'})
    selected = select_cases(rows, all_cases=True)
    assert len(selected) == 110
    assert {r['question_id'] for r in selected} == {r['question_id'] for r in rows[:-1]}


def test_full_selection_covers_all_domains_without_sampling():
    rows = [{'question_id': i, 'db_id': f'domain_{i % 11}'} for i in range(500)]
    selected = select_cases(rows, full=True)
    assert len(selected) == 500
    assert {r['question_id'] for r in selected} == set(range(500))
    assert selected == select_cases(list(reversed(rows)), full=True)
