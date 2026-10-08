import sqlite3
from types import SimpleNamespace
from sql_agent.grounded_retrieval import GroundedRetriever, terms, field_documents


def setup(tmp_path):
    path=tmp_path/'db.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE orders(status TEXT)')
        db.executemany('INSERT INTO orders VALUES (?)',[('Completed',),('Pending',)])
    doc=dict(id='status',database_id='demo',title='orders.status',text='Order status',tables=['orders'],source='csv',version='1')
    field=dict(document=doc,table='orders',column='status',type='TEXT',terms=terms('orders status completed pending'))
    return GroundedRetriever([field],{'demo':SimpleNamespace(database=path,tables=('orders',))},mode='field_values')


def test_grounded_values_scoping_and_no_match(tmp_path):
    r=setup(tmp_path)
    result=r.retrieve('demo','orders with status completed',['orders'])
    assert len(result['hits'])==1 and 'Completed' in result['hits'][0]['text']
    assert r.retrieve('demo','orders with status completed',[])['hits']==[]
    assert r.retrieve('other','orders status completed',['orders'])['hits']==[]
    assert r.retrieve('demo','weather forecast',['orders'])['hits']==[]
    assert r.retrieve('demo','orders status nonexistent',['orders'])['hits']==[]


def test_value_query_does_not_execute_question_sql(tmp_path):
    r=setup(tmp_path)
    r.retrieve('demo',"orders status 'x'); DROP TABLE orders; --",['orders'])
    with sqlite3.connect(r.policies['demo'].database) as db:
        assert db.execute('SELECT count(*) FROM orders').fetchone()[0]==2


def test_only_real_columns_become_documents(tmp_path):
    r=setup(tmp_path)
    folder=tmp_path/'demo'/'database_description';folder.mkdir(parents=True)
    (folder/'orders.csv').write_text('original_column_name,column_description\nstatus,Current order status\ninvented,No such column\n')
    docs,fields=field_documents(tmp_path,r.policies)
    assert len(docs)==1 and fields[0]['column']=='status'


def test_explicit_binding_skips_lookup_and_preserves_no_hit(tmp_path):
    r=setup(tmp_path)
    result=r.retrieve('demo',"orders status completed\nProvided BIRD evidence: orders.status = 'Completed'",['orders'])
    assert result['hits']==[] and result['probes']==[]
    assert result['skipped_fields'][0]['reason']=='explicit_binding_already_provided'


def test_county_does_not_anchor_mailcity(tmp_path):
    from sql_agent.grounded_retrieval import field_anchors
    assert 'county' not in field_anchors(dict(column='MailCity',semantic_name='mailing city'))
    r=setup(tmp_path)
    r.fields[0]['column']='MailCity'
    r.fields[0]['terms']=terms('county schools mail city')
    assert r.retrieve('demo','county schools',['orders'])['candidate_fields']==[]


def test_terms_from_evidence_are_not_lookup_entities(tmp_path):
    from sql_agent.grounded_retrieval import lookup_phrases,complete_matches,split_request
    question,evidence=split_request('Compare Harlan and Jarrod Dixon\nProvided BIRD evidence: MAX(Reputation)')
    phrases=lookup_phrases(question)
    assert 'max' not in phrases
    assert complete_matches(['Jarrod Dixon','Jarrod','Harlan'])==['Jarrod Dixon','Harlan']
    assert 'max' not in lookup_phrases('MAX reputation')


def test_binding_parser_does_not_bind_neighboring_fields():
    from sql_agent.grounded_retrieval import supplied_binding
    field={'column':'status'}
    assert supplied_binding(field,"t.status IN ('C', 'D')")
    assert not supplied_binding(field,"status reported; other = 'C'")
    assert not supplied_binding(field,"other_status = 'C'")
