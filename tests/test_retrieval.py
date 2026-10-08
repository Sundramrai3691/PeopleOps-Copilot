import json
import pytest
from sql_agent.retrieval import KnowledgeRetriever


def doc(ident='revenue', **kwargs):
    return {'id': ident, 'database_id': 'commerce', 'title': 'Net revenue',
            'text': 'Net revenue excludes refunded orders. Amount is stored in cents.',
            'source': 'internal/metrics', 'version': '1', 'tables': ['orders'], **kwargs}


def test_scoped_ranked_provenance_and_no_match():
    r = KnowledgeRetriever([doc(), doc('other', database_id='private'), doc('secret', tables=['payroll'])])
    result = r.retrieve('commerce', 'net revenue', ['orders'])
    assert [h['id'] for h in result['hits']] == ['revenue']
    assert result['hits'][0]['source'] == 'internal/metrics'
    assert len(result['hits'][0]['sha256']) == 64
    assert r.retrieve('commerce', 'xyzzy', ['orders'])['status'] == 'no_match'
    assert r.retrieve('commerce', 'revenue', [])['hits'] == []


def test_budget_and_deterministic_hash():
    r = KnowledgeRetriever([doc('b'), doc('a')])
    first = r.retrieve('commerce', 'revenue', ['orders'], top_k=1)
    assert first['hits'][0]['id'] == 'a'
    assert first == r.retrieve('commerce', 'revenue', ['orders'], top_k=1)
    assert r.retrieve('commerce', 'revenue', ['orders'], max_chars=5)['hits'] == []
    changed = KnowledgeRetriever([doc('a', version='2')]).retrieve('commerce', 'revenue', ['orders'])
    assert changed['hits'][0]['sha256'] != first['hits'][0]['sha256']


def test_invalid_and_optional_config(tmp_path, monkeypatch):
    monkeypatch.delenv('SQL_AGENT_KNOWLEDGE_CONFIG', raising=False)
    assert KnowledgeRetriever.from_env().documents == []
    with pytest.raises(ValueError):
        KnowledgeRetriever([doc(), doc()])
    with pytest.raises(ValueError):
        KnowledgeRetriever([doc(source='')])
    path = tmp_path / 'knowledge.json'
    path.write_text(json.dumps({'documents': [doc()]}))
    monkeypatch.setenv('SQL_AGENT_KNOWLEDGE_CONFIG', str(path))
    assert len(KnowledgeRetriever.from_env().documents) == 1
    path.write_text('{broken')
    with pytest.raises(ValueError):
        KnowledgeRetriever.from_env()
