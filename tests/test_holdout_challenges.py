"""Check that counterexamples distinguish the intended wrong SQL mechanisms."""
import sqlite3
from scripts.evaluation.prepare_frozen_holdout import Suite, ranking, correlated


def test_dense_and_competition_rank_have_different_correct_populations(tmp_path):
    suite=Suite(tmp_path/'suite')
    ranking(suite,0,'dense_top_two');ranking(suite,0,'competition_top_two')
    dense,competition=suite.cases
    assert [r[0] for r in dense['expected']['rows']]==[1,2,3]
    assert [r[0] for r in competition['expected']['rows']]==[1,2]


def test_zero_and_negative_challenges_require_players_without_valid_scores(tmp_path):
    suite=Suite(tmp_path/'suite')
    ranking(suite,0,'all_zero');ranking(suite,0,'negative_with_absent')
    assert len(suite.cases[0]['expected']['rows'])==6
    assert suite.cases[1]['expected']['rows']==[[6,0]]


def test_null_antijoin_and_equal_amount_fanout_are_real_counterexamples(tmp_path):
    suite=Suite(tmp_path/'suite');correlated(suite,0)
    cases={c['family'].split(':')[-1]:c for c in suite.cases}
    with sqlite3.connect(suite.out/'business/risk_0.db') as db:
        not_in=db.execute("SELECT member_id FROM members WHERE member_id NOT IN (SELECT member_id FROM events WHERE state='eligible') ORDER BY member_id").fetchall()
        distinct_sum=db.execute("SELECT m.member_id,COALESCE(SUM(DISTINCT e.units),0)+COALESCE(SUM(DISTINCT b.units),0) FROM members m LEFT JOIN events e ON e.member_id=m.member_id AND e.state='eligible' LEFT JOIN bonuses b ON b.member_id=m.member_id AND b.state='approved' GROUP BY m.member_id ORDER BY m.member_id").fetchall()
    assert cases['null_antijoin']['expected']['rows']==[[3],[4]]
    assert not_in==[]
    assert [list(r) for r in distinct_sum]!=cases['fanout_equal_amounts']['expected']['rows']
