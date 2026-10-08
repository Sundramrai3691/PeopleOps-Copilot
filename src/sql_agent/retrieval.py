"""Bounded, source-scoped BM25 retrieval of operator-curated SQL knowledge."""
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re


def tokens(text):
    return re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]', text.lower().replace('_', ' '))


class KnowledgeRetriever:
    def __init__(self, documents=(), *, mode='bm25', model_path=None, cache_path=None, chunking='window', reranker_path=None):
        if reranker_path and mode != 'hybrid':
            raise ValueError('reranker requires hybrid mode')
        self.reranker_path = reranker_path
        if chunking not in {'window', 'structure'}:
            raise ValueError('chunking must be window or structure')
        self.chunking = chunking
        if mode not in {'bm25','hybrid'}:
            raise ValueError('retrieval mode must be bm25 or hybrid')
        if mode == 'hybrid' and (not model_path or not cache_path):
            raise ValueError('hybrid requires local model and separate vector-cache paths')
        self.mode, self.model_path, self.cache_path = mode, model_path, cache_path
        self.documents = []
        seen = set()
        for document in documents:
            if not isinstance(document, dict):
                raise ValueError('knowledge document must be an object')
            d = dict(document)
            for key in ('id', 'database_id', 'title', 'text', 'source', 'version'):
                if not isinstance(d.get(key), str) or not d[key].strip():
                    raise ValueError('knowledge documents require nonempty ' + key)
            if not isinstance(d.get('tables'), list) or not all(isinstance(t, str) and t for t in d['tables']):
                raise ValueError('knowledge documents require an explicit tables list')
            if (d['database_id'], d['id']) in seen:
                raise ValueError('duplicate knowledge document ID')
            seen.add((d['database_id'], d['id']))
            if len(d['text']) > 12000 or len(d['title']) > 200:
                raise ValueError('knowledge document too large; split it into passages')
            self.documents.append({k: d[k] for k in ('id','database_id','title','text','source','version','tables')})

    @classmethod
    def from_env(cls):
        path = os.getenv('SQL_AGENT_KNOWLEDGE_CONFIG')
        if not path:
            if os.getenv('SQL_AGENT_RETRIEVAL_MODE', 'bm25') != 'bm25':
                raise ValueError('hybrid requires an explicit knowledge configuration')
            return cls()
        with Path(path).open('rb') as stream:
            raw = stream.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError('knowledge configuration exceeds 2 MB')
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get('documents'), list):
            raise ValueError('knowledge configuration requires documents')
        return cls(value['documents'], mode=os.getenv('SQL_AGENT_RETRIEVAL_MODE','bm25'),
                   model_path=os.getenv('SQL_AGENT_EMBEDDING_MODEL_PATH'),
                   cache_path=os.getenv('SQL_AGENT_EMBEDDING_CACHE'),
                   chunking=os.getenv('SQL_AGENT_CHUNKING_MODE', 'window'),
                   reranker_path=os.getenv('SQL_AGENT_RERANKER_MODEL_PATH'))

    def retrieve(self, database_id, question, allowed_tables, *, top_k=4, max_chars=6000):
        if not 1 <= top_k <= 8 or not 1 <= max_chars <= 12000:
            raise ValueError('invalid retrieval budget')
        # Filter before scoring: inaccessible sources cannot influence results.
        docs = [d for d in self.documents if d['database_id'] == database_id
                and set(d['tables']).issubset(allowed_tables)]
        encoder = None
        if self.mode == 'hybrid' and docs:
            from .embeddings import LocalEmbeddings
            encoder = LocalEmbeddings(self.model_path, self.cache_path)
            docs = [chunk for d in docs for chunk in encoder.chunks(d, strategy=self.chunking)]
        counts = [Counter(tokens(d['title'] + ' ' + d['text'])) for d in docs]
        query = set(tokens(question))
        average = sum(sum(c.values()) for c in counts) / max(1, len(counts)) or 1
        ranked = []
        for d, count in zip(docs, counts):
            score = 0.0
            for term in query:
                frequency = count[term]
                if not frequency:
                    continue
                df = sum(term in c for c in counts)
                idf = math.log(1 + (len(docs) - df + .5) / (df + .5))
                score += idf * frequency * 2.5 / (frequency + 1.5 * (.25 + .75 * sum(count.values()) / average))
            if score > 0:
                ranked.append((score, d))
        ranked.sort(key=lambda item: (-item[0], item[1]['id']))
        bm_ranks = {d['id']:i+1 for i, (_,d) in enumerate(ranked[:20])}
        dense_ranks = {}
        if encoder and docs:
            query_vector = encoder.encode([question])[0]
            vectors = encoder.documents(docs)
            if any(len(v)!=len(query_vector) for v in vectors):
                raise ValueError('embedding dimension mismatch')
            dense = sorted(((sum(a*b for a,b in zip(query_vector,v)),d) for v,d in zip(vectors,docs)),
                           key=lambda pair:(-pair[0],pair[1]['id']))
            dense_ranks = {d['id']:i+1 for i,(score,d) in enumerate(dense[:20]) if score>0}
            ranked = [(sum(1/(60+r[d['id']]) for r in (bm_ranks,dense_ranks) if d['id'] in r),d)
                      for d in docs if d['id'] in bm_ranks or d['id'] in dense_ranks]
            ranked.sort(key=lambda pair:(-pair[0],pair[1]['id']))
        reranker_evidence = None
        if self.reranker_path and ranked:
            from .reranking import rerank
            ranked, reranker_evidence = rerank(self.reranker_path, question, ranked)
        hits, remaining = [], max_chars
        for score, d in ranked:
            if len(hits) == top_k:
                break
            # Full passages only; no truncation of business rules mid-sentence.
            if len(d['text']) > remaining:
                continue
            remaining -= len(d['text'])
            digest = hashlib.sha256(json.dumps({k:v for k,v in d.items() if k != 'reranker_score'}, sort_keys=True).encode()).hexdigest()
            hits.append({**d, 'score': round(score, 6), 'sha256': digest})
            if encoder:
                hits[-1].update(bm25_rank=bm_ranks.get(d['id']), dense_rank=dense_ranks.get(d['id']))
        return {'method': 'hybrid_bm25_dense_rrf' if self.mode == 'hybrid' else 'bm25',
                'status': 'matched' if hits else 'no_match', 'scope': database_id,
                'hits': hits, 'context_chars': max_chars - remaining,
                **({'reranker': reranker_evidence} if reranker_evidence else {}),
                **({'embedding_model_sha256':encoder.model_id, 'fusion':'rrf_k60',
                    'vector_store':'sqlite_cache_exact_cosine'} if encoder else {})}
