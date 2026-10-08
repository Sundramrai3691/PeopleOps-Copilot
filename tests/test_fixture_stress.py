from scripts.evaluation.analyze_sql_fixture_stress import stress_case


def test_fixture_stress_exposes_sum_avg_accidental_agreement():
    row = {'id':1,'domain':'commerce','sql_context':'CREATE TABLE t(x INT); INSERT INTO t VALUES (5);',
           'sql':'SELECT AVG(x) FROM t','expected_rows':[[5]]}
    result = stress_case(row, {'baseline':'{"sql":"SELECT SUM(x) FROM t"}',
                              'adapter':'{"sql":"SELECT AVG(x) FROM t"}'})
    assert result['scorable']
    assert result['conditions']['baseline']['original']['correct']
    assert not result['conditions']['baseline']['both_match_source_sql']
    assert result['conditions']['adapter']['both_match_source_sql']


def test_fixture_constraint_failure_is_unscorable():
    row = {'id':1,'domain':'commerce','sql_context':'CREATE TABLE t(x INT PRIMARY KEY); INSERT INTO t VALUES (5);',
           'sql':'SELECT x FROM t','expected_rows':[[5]]}
    result = stress_case(row, {'baseline':'{"sql":"SELECT x FROM t"}'})
    assert result['scorable'] is False
