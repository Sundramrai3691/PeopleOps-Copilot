"""Small declared synthetic workload. Python oracles are never passed to the agent."""
from collections import defaultdict
from datetime import date
from pathlib import Path
import random
import sqlite3

DEMO_QUESTIONS = (
    'What is the total order revenue in cents across all orders?',
    'List every order with its ID, order date and amount in cents, sorted by order ID.',
    'Show total order revenue in cents by calendar month (YYYY-MM), sorted by month.',
)
QUESTIONS = (
    'What is the total amount in cents of paid orders? Return one column named paid_revenue_cents; use zero if none exist.',
    'List paid orders of at least 2500 cents placed on or after 2026-02-01 and before 2026-04-01. Return order_id, ordered_at, amount_cents, sorted by order_id.',
    'For each month containing paid orders, return month (YYYY-MM), order_count and revenue_cents for paid orders only, sorted by month.',
    'For every customer, return customer_id and net_cents: paid order amounts minus approved refund amounts on those paid orders. Count each order and refund once, ignore pending refunds, and use zero for absent or NULL amounts. Include customers without paid orders and sort by customer_id.',
    'List customers who have no paid order. Return customer_id and name, sorted by customer_id. A cancelled order is not a paid order.',
    'Return all customers tied for the highest total paid order amount. Include customers with no paid orders as zero. Return customer_id and paid_cents, sorted by customer_id.',
)
COLUMNS=(('paid_revenue_cents',),('order_id','ordered_at','amount_cents'),('month','order_count','revenue_cents'),('customer_id','net_cents'),('customer_id','name'),('customer_id','paid_cents'))


def fixture(seed):
    rng=random.Random(seed)
    customers=[(i,'Customer '+str(i)) for i in range(1,7)]
    orders=[];refunds=[]
    for i in range(1,17):
        # Customer 6 has only a cancelled order; customer 5 has no orders.
        customer=(i-1)%4+1
        month=rng.choice([1,2,3,4])
        amount=rng.choice([0,1250,2500,3750,5000])
        status='cancelled' if i%5==0 else 'paid'
        orders.append((i,customer,date(2026,month,rng.choice([1,14,28])).isoformat(),amount,status))
        for k in range(rng.randrange(3)):
            refunds.append((len(refunds)+1,i,rng.choice([None,0,250,500]),'approved' if k==0 else 'pending'))
    totals={cid:sum(o[3] for o in orders if o[1]==cid and o[4]=='paid') for cid in range(1,5)}
    target=max(totals.values())+1000
    for cid in (1,2):
        index=next(i for i,o in enumerate(orders) if o[1]==cid and o[4]=='paid')
        o=orders[index];orders[index]=(*o[:3],o[3]+target-totals[cid],o[4])
    orders.append((17,6,'2026-02-01',2500,'cancelled'))
    return {'customers':customers,'orders':orders,'refunds':refunds}


def create_database(path, data):
    path=Path(path)
    if path.exists():raise FileExistsError(path)
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE customers(id INTEGER PRIMARY KEY,name TEXT NOT NULL);
        CREATE TABLE orders(id INTEGER PRIMARY KEY,customer_id INTEGER NOT NULL,ordered_at TEXT NOT NULL,amount_cents INTEGER NOT NULL,status TEXT NOT NULL);
        CREATE TABLE refunds(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL,amount_cents INTEGER,status TEXT NOT NULL);''')
        db.executemany('INSERT INTO customers VALUES(?,?)',data['customers'])
        db.executemany('INSERT INTO orders VALUES(?,?,?,?,?)',data['orders'])
        db.executemany('INSERT INTO refunds VALUES(?,?,?,?)',data['refunds'])


def oracle(case,data):
    paid=[o for o in data['orders'] if o[4]=='paid']
    totals={c[0]:0 for c in data['customers']};net=dict(totals)
    for oid,cid,_,amount,_ in paid:
        totals[cid]+=amount
        net[cid]+=amount-sum((r[2] or 0) for r in data['refunds'] if r[1]==oid and r[3]=='approved')
    if case==0:rows=[[sum(o[3] for o in paid)]]
    elif case==1:rows=[[o[0],o[2],o[3]] for o in paid if o[3]>=2500 and '2026-02-01'<=o[2]<'2026-04-01']
    elif case==2:
        months=defaultdict(lambda:[0,0])
        for o in paid:months[o[2][:7]][0]+=1;months[o[2][:7]][1]+=o[3]
        rows=[[m,*v] for m,v in sorted(months.items())]
    elif case==3:rows=[[c,v] for c,v in sorted(net.items())]
    elif case==4:
        ids={o[1] for o in paid};rows=[[c[0],c[1]] for c in data['customers'] if c[0] not in ids]
    elif case==5:rows=[[c,v] for c,v in sorted(totals.items()) if v==max(totals.values())]
    else:raise ValueError(case)
    return {'columns':list(COLUMNS[case]),'rows':rows}
