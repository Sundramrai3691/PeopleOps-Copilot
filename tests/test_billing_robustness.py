import sqlite3
from scripts.evaluation.check_billing_query_robustness import fixture, score
from scripts.evaluation.transfer_sql_cases import CASES, SCHEMA, expected, fixture as original_fixture


def make(path,data):
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for table,rows in data.items():
            if rows:db.executemany(f'INSERT INTO {table} VALUES ({",".join("?" for _ in rows[0])})',rows)


def test_fractional_percentage_catches_integer_division(tmp_path):
    data=fixture(2);path=tmp_path/'fraction.sqlite';make(path,data)
    case=next(c for c in CASES if c.case_id=='settled_percentage')
    assert expected(case,data)['rows']==((12.5,),)
    integer_sql="SELECT ROUND(100 * COUNT(*) / (SELECT COUNT(*) FROM invoices), 2) AS settled_percent FROM invoices i WHERE EXISTS (SELECT 1 FROM receipts r WHERE r.invoice_id=i.id AND r.state='settled')"
    for v in range(2):
        original=original_fixture(v);old_path=tmp_path/f'original_{v}.sqlite';make(old_path,original)
        assert score(case,integer_sql,old_path,original)['correct'] is True
    assert score(case,integer_sql,path,data)['correct'] is False
    assert score(case,integer_sql.replace('100 *','100.0 *'),path,data)['correct'] is True


def test_null_foreign_key_catches_not_in_trap(tmp_path):
    data=fixture(3);path=tmp_path/'nullable.sqlite';make(path,data)
    case=next(c for c in CASES if c.case_id=='no_settled_receipt')
    assert len(expected(case,data)['rows'])==7
    bad="SELECT id AS invoice_id FROM invoices WHERE id NOT IN (SELECT invoice_id FROM receipts WHERE state='settled') ORDER BY id"
    good="SELECT i.id AS invoice_id FROM invoices i WHERE NOT EXISTS (SELECT 1 FROM receipts r WHERE r.invoice_id=i.id AND r.state='settled') ORDER BY i.id"
    assert score(case,bad,path,data)['correct'] is False
    assert score(case,good,path,data)['correct'] is True
