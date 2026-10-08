"""Local single-pass BIRD evaluation baseline; no gold enters inference.

Prompt design follows phanipal/slonik-7b/scripts/eval_bird_sqlite.py.
This independent adapter uses Ollama GGUF inference and SQLite DDL.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import time
import urllib.request
from collections import Counter
from pathlib import Path


def execute(path, sql):
    started = time.monotonic()
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    db.enable_load_extension(False)
    db.set_authorizer(lambda action, a, b, c, d: sqlite3.SQLITE_DENY if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH) else sqlite3.SQLITE_OK)
    db.set_progress_handler(lambda: int(time.monotonic() - started > 30), 1000)
    try:
        rows = db.execute(sql).fetchmany(100001)
        if len(rows) > 100000:
            raise ValueError('row_cap_exceeded')
        return rows
    finally:
        db.close()


def extract(text):
    match = re.search(r'```(?:sql|sqlite)?\s*(.*?)```', text, re.S | re.I)
    if match:
        return match.group(1).strip()
    match = re.search(r'\b(?:SELECT|WITH)\b.*', text, re.S | re.I)
    return match.group(0).strip() if match else text.strip()


def save_json(path, value):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(path)


def validate_resume(previous, current):
    # Code revisions are recorded separately; inference and grading inputs must match.
    for key in ('model', 'n', 'database_hashes', 'data_sha256', 'options', 'scope', 'grading'):
        if previous.get(key) != current.get(key):
            raise ValueError('Resume configuration differs: ' + key)
    old = [m['digest'] for m in previous['model_details']]
    new = [m['digest'] for m in current['model_details']]
    if not old or old != new:
        raise ValueError('Resume model digest differs or is missing')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--resume', action='store_true', help='Skip saved questions; require identical model, data and generation settings')
    ap.add_argument('--model', default='hf.co/Phani-labs/Slonik-7B-GRPO-GGUF:Q4_K_M')
    args = ap.parse_args()
    raw = (args.data / 'sqlite.jsonl').read_text()
    cases = json.loads(raw) if raw.lstrip().startswith('[') else [json.loads(x) for x in raw.splitlines()]
    assert len(cases) == 500 and len({x['question_id'] for x in cases}) == 500
    root = args.data / 'databases_full'
    args.output.mkdir(parents=True, exist_ok=args.resume)
    hashes = {}
    schemas = {}
    for did in sorted({c['db_id'] for c in cases}):
        path = root / did / (did + '.sqlite')
        h = hashlib.sha256()
        with path.open('rb') as f:
            for block in iter(lambda: f.read(4*1024*1024), b''):
                h.update(block)
        hashes[did] = h.hexdigest()
        schemas[did] = '\n\n'.join(x[0] for x in execute(path, "SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name") if x[0])
    with urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=15) as resp:
        models = json.load(resp)['models']
    manifest = dict(model=args.model, model_details=[m for m in models if m['name']==args.model], n=500, database_hashes=hashes,
        data_sha256=hashlib.sha256(raw.encode()).hexdigest(), script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        options=dict(temperature=0,seed=917,num_predict=512,num_ctx=16384),
        scope='Single-pass Slonik-style prompt, full SQLite DDL plus official evidence, Ollama GGUF; no RAG, repair or semantic Verifier. Not an exact upstream reproduction.',
        grading='Exact set equality; upstream string-normalized equality and multiset equality also recorded. Errors count against all 500.')
    if not manifest['model_details']:
        raise ValueError('Model not installed. Run: ollama pull ' + args.model)
    manifest_path = args.output/'manifest.json'
    records=[]
    if args.resume:
        previous = json.loads(manifest_path.read_text())
        validate_resume(previous, manifest)
        save_json(args.output/'resume_manifest.json', manifest)
        for case in cases:
            saved = args.output/f"{case['question_id']}.json"
            if saved.exists():
                record = json.loads(saved.read_text())
                if record['question_id'] != case['question_id'] or record['db_id'] != case['db_id']:
                    raise ValueError('Saved record identity mismatch')
                records.append(record)
    else:
        save_json(manifest_path, manifest)
    completed_ids = {r['question_id'] for r in records}
    print(f'Starting: {len(records)}/500 already saved', flush=True)
    for case in cases:
        if case['question_id'] in completed_ids:
            continue
        did=case['db_id']; path=root/did/(did+'.sqlite')
        prompt=f"You are a SQLite expert. Given the schema below, write a single SQL query.\n\n### Schema:\n{schemas[did]}\n\n### Question:\n{case['question']}\n"
        if case.get('evidence'): prompt+=f"\n### Hint:\n{case['evidence']}\n"
        prompt+='\nReturn ONLY the SQL query, no explanation.'
        record=dict(question_id=case['question_id'],db_id=did,difficulty=case.get('difficulty'),prompt=prompt,correct=False,bag_match=False,upstream_match=False)
        started=time.monotonic()
        try:
            payload=dict(model=args.model,stream=False,options=manifest['options'],messages=[dict(role='user',content=prompt)],keep_alive='10m')
            req=urllib.request.Request('http://127.0.0.1:11434/api/chat',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=180) as resp: response=json.load(resp)
            record['response']=response
            record['sql']=extract(response['message']['content'])
            record['generation_seconds']=time.monotonic()-started
            try:
                predicted=execute(path,record['sql'])
            except Exception as exc:
                record.update(status='execution_error',error=str(exc))
                predicted=None
            try:
                expected=execute(path,case['SQL'])
                record['gold_rows']=len(expected)
                if predicted is not None:
                    record['correct']=set(predicted)==set(expected)
                    record['bag_match']=Counter(predicted)==Counter(expected)
                    norm=lambda rows:{tuple(str(v) if v is not None else 'NULL' for v in row) for row in rows}
                    record['upstream_match']=norm(predicted)==norm(expected)
                    record['status']='correct' if record['correct'] else 'wrong'
                    record['predicted_rows']=len(predicted)
            except Exception as exc:
                record.update(status='reference_error',reference_error=str(exc))
        except Exception as exc:
            record.update(status='inference_error',error=str(exc))
        record['elapsed_seconds']=time.monotonic()-started
        records.append(record)
        save_json(args.output/f"{case['question_id']}.json", record)
        summary=dict(completed=len(records),total=500,correct=sum(r['correct'] for r in records),statuses=dict(Counter(r['status'] for r in records)),
            bag_matches=sum(r['bag_match'] for r in records),upstream_matches=sum(r['upstream_match'] for r in records),
            by_database={d:dict(n=sum(r['db_id']==d for r in records),correct=sum(r['correct'] for r in records if r['db_id']==d)) for d in schemas})
        summary['accuracy_completed'] = summary['correct'] / len(records)
        summary['accuracy_500'] = summary['correct'] / 500 if len(records) == 500 else None
        save_json(args.output/'summary.json', summary)
        if len(records)%10==0: print(json.dumps({k:summary[k] for k in ('completed','correct','statuses')}),flush=True)

    print(f"Finished: {sum(r['correct'] for r in records)}/{len(records)} correct. Results: {args.output}", flush=True)

if __name__=='__main__': main()
