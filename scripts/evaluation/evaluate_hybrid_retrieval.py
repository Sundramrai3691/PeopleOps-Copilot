"""Versioned synthetic retrieval evaluation; not SQL accuracy or production SLO."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import time

from sql_agent.retrieval import KnowledgeRetriever


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--knowledge', type=Path, required=True)
    p.add_argument('--queries', type=Path, required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--cache', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--database', default='demo')
    p.add_argument('--chunking', choices=['window', 'structure'], default='window')
    p.add_argument('--reranker', help='Optional local cross-encoder model directory')
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    docs = json.loads(args.knowledge.read_text())['documents']
    dataset = json.loads(args.queries.read_text())
    tables = sorted({t for d in docs if d['database_id']==args.database for t in d['tables']})
    repo = Path(__file__).resolve().parents[2]
    sources = {str(f.relative_to(repo)):hashlib.sha256(f.read_bytes()).hexdigest() for f in (repo/'src').rglob('*.py')}
    summaries = {}
    variants = ['bm25', 'hybrid'] + (['hybrid_reranked'] if args.reranker else [])
    for mode in variants:
        retriever = KnowledgeRetriever(docs, mode='bm25' if mode == 'bm25' else 'hybrid',
            model_path=args.model, cache_path=args.cache, chunking=args.chunking,
            reranker_path=args.reranker if mode == 'hybrid_reranked' else None)
        for phase in ('first_pass','warm_cache'):
            rows = []
            for case in dataset['queries']:
                tick = time.perf_counter()
                evidence = retriever.retrieve(args.database, case['question'], tables)
                seconds = time.perf_counter()-tick
                ids = list(dict.fromkeys(h.get('parent_id',h['id']) for h in evidence['hits']))
                rank = ids.index(case['relevant_id'])+1 if case['relevant_id'] in ids else None
                rows.append({'question':case['question'],'relevant_id':case['relevant_id'],
                             'rank':rank,'seconds':seconds,'evidence':evidence})
            latencies = sorted(r['seconds'] for r in rows)
            summary = {'queries':len(rows),'hit_at_1':sum(r['rank']==1 for r in rows)/len(rows),
                       'recall_at_4':sum(r['rank'] is not None for r in rows)/len(rows),
                       'mrr_at_4':sum(1/r['rank'] if r['rank'] else 0 for r in rows)/len(rows),
                       'p95_seconds':latencies[math.ceil(.95*len(rows))-1]}
            name=mode+'_'+phase
            (args.output/(name+'.json')).write_text(json.dumps(rows,indent=2))
            summaries[name]=summary
    assert all(hashlib.sha256((repo/name).read_bytes()).hexdigest()==h for name,h in sources.items())
    (args.output/'manifest.json').write_text(json.dumps({'dataset_version':dataset['version'],
        'dataset_sha256':hashlib.sha256(args.queries.read_bytes()).hexdigest(),
        'knowledge_sha256':hashlib.sha256(args.knowledge.read_bytes()).hexdigest(),
        'source_sha256':sources,'scope':dataset['scope'],'sources_unchanged':True,
        'chunking':args.chunking, 'reranker_enabled':bool(args.reranker)},indent=2))
    (args.output/'summary.json').write_text(json.dumps(summaries,indent=2))
    print(json.dumps(summaries,indent=2))


if __name__ == '__main__':
    main()
