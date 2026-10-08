import sqlite3
import pytest
from sql_agent.commerce_catalog import REFERENCES,make_task
from sql_agent.data_agent import SQLAgentSession
from sql_agent.bounded_runtime import ActionProposal

CASES=[([],[]),([(1,10000),(2,8000)],[(1,1000,'approved'),(1,2000,'pending')]),
       ([(1,10000),(2,8000)],[(1,1000,'approved'),(1,2000,'approved')]),
       ([(1,0),(2,2500)],[(2,500,'pending')])]

@pytest.mark.parametrize('orders,refunds',CASES)
@pytest.mark.parametrize('metric',list(REFERENCES))
def test_catalog_matches_independent_python_calculation(orders,refunds,metric,tmp_path):
 db=sqlite3.connect(':memory:')
 try:
  db.executescript('CREATE TABLE orders(id INTEGER PRIMARY KEY,amount_cents INTEGER); CREATE TABLE refunds(order_id INTEGER,amount_cents INTEGER,status TEXT);')
  db.executemany('INSERT INTO orders VALUES(?,?)',orders);db.executemany('INSERT INTO refunds VALUES(?,?,?)',refunds);db.commit()
  gross=sum(o[1] for o in orders);approved=sum(r[1] for r in refunds if r[2]=='approved')
  expected={'gross_revenue':gross,'approved_refunds':approved,'net_revenue':gross-approved}[metric]
  session=SQLAgentSession(db,make_task(metric,tmp_path/'source.sqlite').contract)
  p=ActionProposal('REPAIR','sql_query',{'sql':REFERENCES[metric]})
  assert session.execute(p)['rows']==((expected,),)
 finally:db.close()


def test_wrong_join_and_wrong_refund_total_rejected_by_business_verifier(tmp_path):
 db=sqlite3.connect(':memory:');db.executescript('CREATE TABLE orders(id INTEGER,amount_cents INTEGER); CREATE TABLE refunds(order_id INTEGER,amount_cents INTEGER,status TEXT); INSERT INTO orders VALUES(1,10000),(2,8000); INSERT INTO refunds VALUES(1,1000,"approved"),(1,2000,"approved");')
 try:
  s=SQLAgentSession(db,make_task('net_revenue',tmp_path/'x').contract)
  for sql in ["SELECT SUM(o.amount_cents)-SUM(r.amount_cents) AS net_revenue_cents FROM orders o LEFT JOIN refunds r ON o.id=r.order_id", "SELECT SUM(amount_cents) AS net_revenue_cents FROM refunds WHERE status='approved'"]:
   p=ActionProposal('REPAIR','sql_query',{'sql':sql})
   assert s.verify(p,s.execute(p))==(False,'business_result_mismatch')
 finally:db.close()


def test_verified_metric_does_not_allow_question_rebinding(tmp_path):
 t=make_task('net_revenue',tmp_path/'x')
 with pytest.raises(ValueError):t.validate_question('Count customers instead')
 assert not t.contract.fallback_to_verified_query
