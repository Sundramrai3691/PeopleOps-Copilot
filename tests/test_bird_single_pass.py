import copy
import json
import sqlite3

import pytest

from scripts.evaluation.evaluate_bird_single_pass import execute, extract, save_json, validate_resume


def test_readonly_executor(tmp_path):
    path = tmp_path / 'db.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE t(x)')
        db.execute('INSERT INTO t VALUES (1)')
    assert execute(path, 'SELECT x FROM t') == [(1,)]
    for sql in ('DELETE FROM t', "ATTACH DATABASE ':memory:' AS other"):
        with pytest.raises(sqlite3.DatabaseError):
            execute(path, sql)
    assert execute(path, 'SELECT x FROM t') == [(1,)]


def test_resume_rejects_changed_model_data_or_generation():
    original = dict(model='slonik', n=500, database_hashes={'db':'hash'},
                    data_sha256='data', options={'num_predict':512}, scope='single',
                    grading='set', model_details=[{'digest':'weights'}])
    validate_resume(original, copy.deepcopy(original))
    for key, value in [('database_hashes', {}), ('options', {}),
                       ('model_details', [{'digest':'different'}])]:
        changed = copy.deepcopy(original)
        changed[key] = value
        with pytest.raises(ValueError):
            validate_resume(original, changed)


def test_extract_and_atomic_record(tmp_path):
    assert extract('```sql\nSELECT 1;\n```') == 'SELECT 1;'
    path = tmp_path / 'record.json'
    save_json(path, {'correct':False})
    assert json.loads(path.read_text()) == {'correct':False}
    assert not path.with_suffix('.json.tmp').exists()
