import sqlite3
import pytest
from sql_agent.data_agent import SQLAgentSession,DataContract
from sql_agent.bounded_runtime import ActionProposal

@pytest.mark.parametrize('sql,expected',[
 ("SELECT 'Ada' LIKE 'A%' AS value",1),
 ("SELECT strftime('%Y','2025-01-02') AS value",'2025'),
 ("SELECT instr('abc','b') AS value",2),
 ("SELECT julianday('2025-01-02')-julianday('2025-01-01') AS value",1.0),
 ("SELECT datetime('2025-01-02','+1 day') AS value",'2025-01-03 00:00:00'),
 ("SELECT date('2025-01-02','+1 day') AS value",'2025-01-03'),
])
def test_builtin_read_functions_work_under_production_authorizer(sql,expected):
 db=sqlite3.connect(':memory:')
 try:
  session=SQLAgentSession(db,DataContract(('value',)))
  p=ActionProposal('REPAIR','sql_query',{'sql':sql})
  assert session.authorize(p)[0]
  assert session.execute(p)['rows']==((expected,),)
 finally:db.close()

@pytest.mark.parametrize('sql',["SELECT load_extension('bad') AS value", "SELECT randomblob(100000000) AS value"])
def test_external_and_unbounded_allocating_functions_remain_denied(sql):
 db=sqlite3.connect(':memory:')
 try:
  session=SQLAgentSession(db,DataContract(('value',)))
  with pytest.raises(sqlite3.DatabaseError):session.execute(ActionProposal('REPAIR','sql_query',{'sql':sql}))
 finally:db.close()
