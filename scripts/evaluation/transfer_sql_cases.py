"""New billing schema and questions, frozen before model evaluation.

Operator motifs overlap development tasks. This tests schema/task transfer, not
unseen SQL operators or absence from model training. Oracles are Python arithmetic.
"""
import sqlite3
from sql_agent.paired_benchmark import Case

SCHEMA = '''CREATE TABLE invoices(id INTEGER PRIMARY KEY, account TEXT, amount INTEGER, due_day TEXT);
CREATE TABLE receipts(id INTEGER PRIMARY KEY, invoice_id INTEGER, amount INTEGER, state TEXT);
CREATE TABLE credits(id INTEGER PRIMARY KEY, invoice_id INTEGER, amount INTEGER);'''

CASES = (
    Case('settled_by_account','billing','For every account in invoices, sum receipt amounts with state settled. Each receipt counts once. NULL amounts count zero. Include accounts with no settled receipts as zero. Return account, settled_amount ordered by account.',('account','settled_amount')),
    Case('invoice_balance','billing','For every invoice, compute balance = invoice amount minus settled receipt amounts minus all credit amounts. Sum each receipt and credit exactly once; NULL and absent amounts are zero. Return invoice_id and balance ordered by invoice_id.',('invoice_id','balance')),
    Case('no_settled_receipt','billing','List invoice ids as invoice_id with no receipt whose state is settled. Pending receipts do not disqualify an invoice; settled receipts with NULL amount do disqualify it. Order by invoice_id.',('invoice_id',)),
    Case('largest_ties','billing','For each account, list every invoice tied for that account largest invoice amount. Return account, invoice_id, amount ordered by account then invoice_id.',('account','invoice_id','amount')),
    Case('overdue_by_account','billing','For every account, count invoices with due_day strictly before 2026-03-01. Ignore payment state. NULL due_day is not overdue. Include zero counts. Return account, overdue_count ordered by account.',('account','overdue_count')),
    Case('receipt_count','billing','For every account, count settled receipt records, including records with NULL amount. Multiple settled receipts on one invoice count separately. Include accounts with none as zero. Return account, receipt_count ordered by account.',('account','receipt_count')),
    Case('positive_balances','billing','Count invoices with invoice amount minus settled receipt amounts minus credits strictly greater than zero. Each amount counts once, and NULL or absent sums are zero. Return one positive_invoices value.',('positive_invoices',)),
    Case('settled_percentage','billing','Return the percentage of invoices having at least one settled receipt, including settled receipts with NULL amount. Each invoice counts once in numerator and denominator; round to two decimal places. Return one settled_percent value.',('settled_percent',)),
)


def fixture(variant):
    return {
        'invoices': [(11,'north',100+variant,'2026-01-01'),(12,'north',100+variant,'2026-03-01'),
                     (13,'south',200,'2026-02-28'),(14,'west',0,None),(15,'south',50,'2026-04-01')],
        'receipts': [(1,11,30,'settled'),(2,11,20+variant,'settled'),(3,12,999,'pending'),
                     (4,13,250,'settled'),(5,15,None,'settled')]
                    + ([(6,12,60,'settled')] if variant else []),
        'credits': [(1,11,5),(2,11,7+variant),(3,13,20)],
    }


def create_database(path, data):
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for table, rows in data.items():
            db.executemany(f'INSERT INTO {table} VALUES ({",".join("?" for _ in rows[0])})',rows)


def expected(case, data):
    inv = data['invoices']
    settled = [r for r in data['receipts'] if r[3]=='settled']
    sums = {i[0]: sum(r[2] or 0 for r in settled if r[1]==i[0]) for i in inv}
    balance = {i[0]: i[2]-sums[i[0]]-sum(c[2] or 0 for c in data['credits'] if c[1]==i[0]) for i in inv}
    accounts = sorted({i[1] for i in inv})
    ids = {r[1] for r in settled}
    answers = {
        'settled_by_account': [(a,sum(sums[i[0]] for i in inv if i[1]==a)) for a in accounts],
        'invoice_balance': sorted(balance.items()),
        'no_settled_receipt': [(i[0],) for i in inv if i[0] not in ids],
        'largest_ties': sorted((i[1],i[0],i[2]) for i in inv if i[2]==max(j[2] for j in inv if j[1]==i[1])),
        'overdue_by_account': [(a,sum(i[1]==a and i[3] is not None and i[3]<'2026-03-01' for i in inv)) for a in accounts],
        'receipt_count': [(a,sum(r[1]==i[0] for i in inv if i[1]==a for r in settled)) for a in accounts],
        'positive_balances': [(sum(v>0 for v in balance.values()),)],
        'settled_percentage': [(round(100*sum(i[0] in ids for i in inv)/len(inv),2),)],
    }
    return {'columns':case.columns,'rows':tuple(answers[case.case_id])}
