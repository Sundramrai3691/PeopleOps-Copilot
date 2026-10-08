"""A concurrent writer must not change the data between collection and repair."""
import sqlite3
import pytest
from sql_agent.bounded_runtime import ActionProposal
from sql_agent.data_agent import SQLAgentSession, DataContract


def test_general_sql_session_keeps_collection_snapshot(tmp_path):
    path=tmp_path/'source.sqlite'
    with sqlite3.connect(path) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE readings(value INTEGER)')
        writer.execute('INSERT INTO readings VALUES (1)');writer.commit()
        reader=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        try:
            session=SQLAgentSession(reader,DataContract(('value',)))
            session.collect()
            with pytest.raises(sqlite3.OperationalError):
                session.execute(ActionProposal('REPAIR','sql_query',{'sql':'SELECT missing FROM readings'}))
            # Another connection commits while the model is preparing its repair.
            writer.execute('UPDATE readings SET value=2');writer.commit()
            session.collect(reason='retry after execution error')
            result=session.execute(ActionProposal('REPAIR','sql_query',{'sql':'SELECT value FROM readings'}))
            assert result['rows']==((1,),)
        finally:
            reader.close()
        assert writer.execute('SELECT value FROM readings').fetchall()==[(2,)]
