import sqlite3
import pytest
from sql_agent.data_agent import DataContract, SQLAgentSession
from sql_agent.bounded_runtime import ActionProposal
from sql_agent.execution_record import ContractSnapshot


def test_fixed_contract_hash_remains_compatible():
    contract=DataContract(('total',))
    # Frozen legacy format, not a reconstruction from today's dataclass fields.
    old={'columns': ('total',), 'non_null': (), 'min_rows': 0, 'max_rows': 1000,
         'version': '1', 'verification_sql': '', 'fallback_to_verified_query': False}
    assert contract.snapshot().sha256==ContractSnapshot.capture('sql_query',old).sha256
    assert contract.snapshot().sha256 == '8cb8fa88e7f1a685d09407e89349b6f94c17cce0ebc1f29018fa185d1c244535'


def test_dynamic_contract_supports_scalar_list_and_grouped_outputs():
    conn=sqlite3.connect(':memory:')
    conn.executescript("CREATE TABLE orders(id INTEGER, amount INTEGER, month TEXT); INSERT INTO orders VALUES(1,10,'2026-01'),(2,20,'2026-01'),(3,30,'2026-02');")
    session=SQLAgentSession(conn,DataContract((),dynamic_columns=True,max_rows=20))
    try:
        for sql,columns,rows in [
            ('SELECT SUM(amount) AS total FROM orders',('total',),((60,),)),
            ('SELECT id,amount FROM orders ORDER BY id',('id','amount'),((1,10),(2,20),(3,30))),
            ('SELECT month,SUM(amount) AS revenue FROM orders GROUP BY month ORDER BY month',('month','revenue'),(('2026-01',30),('2026-02',30)))]:
            proposal=ActionProposal('REPAIR','sql_query',{'sql':sql})
            assert session.authorize(proposal)[0]
            output=session.execute(proposal)
            assert session.verify(proposal,output)[0]
            assert output=={'columns':columns,'rows':rows}
        assert not session.authorize(ActionProposal('REPAIR','sql_query',{'sql':'DELETE FROM orders'}))[0]
        p=ActionProposal('REPAIR','sql_query',{'sql':'SELECT id AS x,amount AS x FROM orders'})
        assert not session.verify(p,session.execute(p))[0]
    finally:conn.close()


def test_dynamic_contract_cannot_enable_business_release_or_unbounded_columns():
    with pytest.raises(ValueError):DataContract((),dynamic_columns=True,verification_sql='SELECT 1')
    with pytest.raises(ValueError):DataContract(())
    conn=sqlite3.connect(':memory:')
    session=SQLAgentSession(conn,DataContract((),dynamic_columns=True,max_rows=2))
    p=ActionProposal('REPAIR','sql_query',{'sql':'SELECT 1'})
    assert not session.verify(p,{'columns':tuple('c'+str(i) for i in range(51)),'rows':()})[0]
    assert not session.verify(p,{'columns':('x',),'rows':((1,),(2,),(3,))})[0]
    conn.close()
