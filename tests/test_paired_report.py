import json
import pytest
from scripts.evaluation.render_paired_sql_report import load_complete


def artifact(tmp_path):
    rows, schedule, methods = [], [], {}
    for method in ('one_shot','execution_retry','checked_agent'):
        methods[method] = {}
        for profile in ('clean','transient'):
            rows.append(dict(case_id='example',variant=0,method=method,profile=profile,
                             trial=0,accepted_correct=True))
            schedule.append(['example',0,method,profile,0,'timeout'])
            methods[method][profile] = {'episodes':1,'accepted_correct':1}
    (tmp_path/'manifest.json').write_text(json.dumps({'schedule':schedule,'episodes_planned':6}))
    (tmp_path/'summary.json').write_text(json.dumps({'episodes':6,'by_method':methods}))
    for i,row in enumerate(rows):
        (tmp_path/f'episode_{i:04d}.json').write_text(json.dumps(row))


def test_final_report_refuses_partial_or_duplicate_run(tmp_path):
    artifact(tmp_path)
    assert len(load_complete(tmp_path)[2]) == 6
    original = (tmp_path/'episode_0000.json').read_text()
    (tmp_path/'episode_0000.json').unlink()
    with pytest.raises(ValueError): load_complete(tmp_path)
    (tmp_path/'episode_0000.json').write_text(original)
    (tmp_path/'episode_0006.json').write_text(original)
    with pytest.raises(ValueError): load_complete(tmp_path)


def test_report_refuses_summary_that_overstates_success(tmp_path):
    artifact(tmp_path)
    path = tmp_path/'summary.json'
    summary = json.loads(path.read_text())
    summary['by_method']['checked_agent']['clean']['accepted_correct'] = 2
    path.write_text(json.dumps(summary))
    with pytest.raises(ValueError,match='disagree'): load_complete(tmp_path)
