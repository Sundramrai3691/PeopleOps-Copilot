"""Independent hand-calculated business answers protect the evaluation oracle."""
import json
from pathlib import Path
import sqlite3

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / 'data/benchmarks/commerce_v1.json').read_text())


@pytest.mark.parametrize('case', [c for c in CASES if c['expected'] == 'KEEP'], ids=lambda c: c['id'])
def test_reference_matches_hand_calculated_business_answer(case):
    db = sqlite3.connect(':memory:')
    try:
        db.executescript(case['setup_sql'])
        cur = db.execute(case['gold_sql'])
        assert [c[0] for c in cur.description] == case['contract']['columns']
        assert [list(row) for row in cur.fetchall()] == case['expected_rows']
        assert case['setup_sql'] == (ROOT / 'data/demo/commerce.sql').read_text()
    finally:
        db.close()


def test_fixture_exposes_refund_join_fanout_bug():
    db = sqlite3.connect(':memory:')
    try:
        db.executescript(CASES[0]['setup_sql'])
        wrong = db.execute("SELECT SUM(o.gross_cents)-SUM(COALESCE(r.amount_cents,0)) "
            "FROM orders o LEFT JOIN refunds r ON r.order_id=o.order_id "
            "WHERE o.status='paid' AND o.ordered_at >= '2026-01-01' AND o.ordered_at < '2026-02-01'").fetchone()[0]
        assert wrong != 18000
    finally:
        db.close()
