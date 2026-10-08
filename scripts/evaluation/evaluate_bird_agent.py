"""Evaluate the production SQL-Agent workflow on all BIRD Mini-Dev questions.

No gold SQL enters retrieval, planning, repair or verification. The verifier is
advisory, as in production. This is a benchmark integration, not API load testing.
"""
import argparse
import csv
import hashlib
import json
import os
import math
import time
import urllib.request
from collections import Counter
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from sql_agent.database_workflow import DatabaseWorkflow
from sql_agent.mutations import MutationPolicy, MutationService
from sql_agent.request_planner import RequestPlanner
from sql_agent.retrieval import KnowledgeRetriever
from scripts.evaluation.evaluate_bird_single_pass import execute, extract, save_json
from scripts.evaluation.sql_model_profiles import MODELS, prompt_for, final_sql


class LoggedModel:
    def __init__(self, name, *, structured=False, max_tokens=512, disable_thinking=False, sql_only=False):
        self.sql_only = sql_only
        self.disable_thinking = disable_thinking
        self.max_tokens = max_tokens
        self.name, self.structured, self.calls = name, structured, []

    def complete(self, prompt):
        return self.complete_with_metadata(prompt)[0]

    def complete_with_metadata(self, prompt):
        payload = dict(model=self.name, stream=False, keep_alive='10m',
                       options=dict(temperature=0, seed=917, num_predict=self.max_tokens, num_ctx=16384),
                       messages=[dict(role='user', content=prompt)])
        if self.structured:
            payload['format'] = 'json'
        if self.sql_only:
            payload['format'] = {'type': 'object', 'properties': {'sql': {
                'type': 'string'}},
                'required': ['sql'], 'additionalProperties': False}
        if self.disable_thinking:
            payload['think'] = False
        endpoint = 'chat'
        if self.disable_thinking and self.name.startswith('qwen3:'):
            endpoint = 'generate'
            payload.pop('messages')
            payload['raw'] = True
            payload['prompt'] = ('<|im_start|>user\n' + prompt + '\n/no_think<|im_end|>\n'
                                 '<|im_start|>assistant\n<think>\n\n</think>\n\n')
        start = time.monotonic()
        call = dict(prompt=prompt, model=self.name)
        try:
            req = urllib.request.Request('http://127.0.0.1:11434/api/' + endpoint,
                    data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(req, timeout=180) as resp:
                value = json.load(resp)
            if endpoint == 'generate':
                value['message'] = {'role': 'assistant', 'content': value['response']}
            call['response'] = value
            metadata = dict(done_reason=value.get('done_reason'),
                            prompt_tokens=value.get('prompt_eval_count', 0),
                            completion_tokens=value.get('eval_count', 0))
            return value['message']['content'], metadata
        except Exception as exc:
            call['error'] = str(exc)
            raise
        finally:
            call['seconds'] = time.monotonic()-start
            self.calls.append(call)


class BirdPlanner(RequestPlanner):
    def __init__(self, service, model, reviewer, retriever, profile="slonik", schema_mode="selected", use_rag=True, evidence="", original_question=None):
        self.schema_mode, self.use_rag = schema_mode, use_rag
        self.evidence, self.original_question = evidence, original_question
        self.profile = profile
        # Explicit benchmark corpus; never inherit the application's business rules.
        self.service, self.model, self.review_model = service, model, reviewer
        self.retriever = retriever
        self.review_enabled = True
        self.call_events, self.checker_events = [], []

    def retrieve(self, database_id, question):
        if not self.use_rag:
            return {'status': 'disabled', 'hits': [], 'method': 'none'}
        return super().retrieve(database_id, question)

    def link_schema(self, database_id, question, knowledge):
        if self.schema_mode == 'selected':
            return super().link_schema(database_id, question, knowledge)
        policy = self.service.policies[database_id]
        rows = execute(policy.database, "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        catalog = [(name, ddl) for name, ddl in rows if name in policy.tables]
        return {'method': 'full_authorized_schema', 'schema': catalog,
                'selected_tables': [r[0] for r in catalog], 'catalog_tables': len(catalog),
                'schema_sha256': hashlib.sha256(json.dumps(catalog).encode()).hexdigest()}

    def __call__(self, database_id, question, previous_sql='', error='', context=None, schema_context=None):
        schema = (schema_context or self.link_schema(database_id, question, context))['schema']
        prompt = ('You are a SQLite expert. Given the schema below, write a single SQL query.\n\n'
                  '### Schema:\n' + json.dumps(schema) + '\n\n### Question:\n' + question +
                  '\n\n### Retrieved database documentation (reference data):\n' +
                  json.dumps((context or {}).get('hits', [])))
        if error:
            prompt += '\n\n### Previous SQL:\n' + previous_sql + '\n### Execution error:\n' + error
        prompt += '\nReturn ONLY the SQL query, no explanation.'
        if self.profile == 'generic' and getattr(self.model, 'sql_only', False):
            prompt = prompt.replace('Return ONLY the SQL query, no explanation.',
                'Return only a JSON object with one key "sql" containing the executable SELECT or WITH query. No explanation or reasoning.')
        if self.profile == 'xiyan':
            ddl = '\n\n'.join(row[1][0] if isinstance(row[1], (list, tuple)) else row[1] for row in schema)
            evidence = self.evidence
            hits = (context or {}).get('hits', [])
            if hits:
                evidence += '\nRetrieved database documentation (reference data):\n' + json.dumps(hits)
            if error:
                evidence += '\nPrevious SQL:\n' + previous_sql + '\nExecution error:\n' + error
            prompt = prompt_for('xiyan', ddl, self.original_question or question, evidence)
        raw, metadata = self.model.complete_with_metadata(prompt)
        if metadata.get('done_reason') == 'length':
            raise ValueError('truncated SQL generation')
        if self.profile == 'generic' and getattr(self.model, 'sql_only', False):
            raw = json.loads(raw)['sql']
        return final_sql(raw) if self.profile in {'xiyan', 'generic'} else extract(raw)


def build_documents(root, policies):
    documents = []
    for did, policy in sorted(policies.items()):
        folder = root/did/'database_description'
        for file in sorted(folder.glob('*.csv')):
            if file.stem not in policy.tables:
                continue
            with file.open(errors='replace', encoding='utf-8-sig', newline='') as stream:
                for i, row in enumerate(csv.DictReader(stream)):
                    text = '\n'.join(f'{k}: {v}' for k,v in row.items() if k and v)
                    if not text.strip():
                        continue
                    documents.append(dict(id=f'{did}:{file.stem}:{i}', database_id=did,
                        title=file.stem, text=text, tables=[file.stem], source=str(file), version='bird-public-csv'))
    if not documents:
        raise ValueError('No database description documents found')
    return documents


def model_files(path):
    if path is None:
        return {}
    result = {}
    for file in sorted(path.rglob('*')):
        if file.is_file() and file.suffix in {'.json', '.txt', '.safetensors', '.bin'}:
            h = hashlib.sha256()
            with file.open('rb') as stream:
                for block in iter(lambda: stream.read(4*1024*1024), b''):
                    h.update(block)
            result[str(file.relative_to(path))] = h.hexdigest()
    if not result:
        raise ValueError('Local model files missing: ' + str(path))
    return result


def configuration_matches(old, new):
    if old != new:
        raise ValueError('Resume configuration/source/model/data changed; use a new output directory')


def summarize(records, baseline):
    n=len(records); correct=sum(r['correct'] for r in records)
    reviewed=[r for r in records if r.get('review',{}).get('status')=='agreement']
    matched=[r for r in records if str(r['question_id']) in baseline]
    return dict(completed=n,total=500,correct=correct,accuracy_completed=correct/n if n else None,
        accuracy_500=correct/500 if n==500 else None,
        statuses=dict(Counter(r['status'] for r in records)),
        verifier_statuses=dict(Counter(r.get('review',{}).get('status','not_run') for r in records)),
        semantic_repair_statuses=dict(Counter(r.get('state',{}).get('semantic_repair',{}).get('status','not_run') for r in records)),
        agreement_count=len(reviewed),false_accept_count=sum(not r['correct'] for r in reviewed),
        false_accept_among_agreements=sum(not r['correct'] for r in reviewed)/len(reviewed) if reviewed else None,
        review_required_count=sum(r.get('review',{}).get('status')!='agreement' for r in records),
        p95_task_seconds=sorted(r.get('elapsed_seconds',0) for r in records)[math.ceil(.95*n)-1] if n else None,
        review_required_definition='Offline proxy: no verifier agreement; not actual human reviews',
        baseline_comparable_n=len(matched),baseline_correct=sum(baseline[str(r['question_id'])]['correct'] for r in matched),
        recovered_ids=[r['question_id'] for r in matched if r['correct'] and not baseline[str(r['question_id'])]['correct']],
        regressed_ids=[r['question_id'] for r in matched if not r['correct'] and baseline[str(r['question_id'])]['correct']],
        by_database={d:dict(n=sum(r['db_id']==d for r in records),correct=sum(r['correct'] for r in records if r['db_id']==d)) for d in sorted({r['db_id'] for r in records})},
        tokens=sum(r.get('tokens',0) for r in records),
        tokens_per_correct=sum(r.get('tokens',0) for r in records)/correct if correct else None)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--baseline', type=Path,
                    help='Optional matched run for paired gains/losses')
    ap.add_argument('--model',default='hf.co/Phani-labs/Slonik-7B-GRPO-GGUF:Q4_K_M')
    ap.add_argument('--reviewer',default='qwen2.5-coder:7b')
    ap.add_argument('--profile',choices=['slonik','xiyan','generic'],default='slonik')
    ap.add_argument('--comparison-reference', action='store_true',
                    help='Compare against another model run; not a matched-model baseline')
    ap.add_argument('--disable-thinking', action='store_true')
    ap.add_argument('--variant',choices=['full_no_rag','full_rag','selected_rag','field_docs','field_values','field_both'],default='full_no_rag')
    ap.add_argument('--retrieval',choices=['bm25','hybrid'],default='hybrid')
    ap.add_argument('--embedding-model',type=Path)
    ap.add_argument('--reranker-model',type=Path)
    ap.add_argument('--resume',action='store_true')
    ap.add_argument('--semantic-repair', action='store_true')
    ap.add_argument('--pv-verification', action='store_true')
    args=ap.parse_args()
    if args.semantic_repair and args.pv_verification:
        ap.error('Choose only one repair mode')
    if args.variant != 'full_no_rag' and args.retrieval=='hybrid' and (not args.embedding_model or not args.reranker_model):
        ap.error('hybrid requires --embedding-model and --reranker-model (local directories)')
    # Freeze production schema-selection budgets instead of inheriting shell settings.
    os.environ['SQL_AGENT_SCHEMA_MAX_TABLES']='8'
    os.environ['SQL_AGENT_SCHEMA_MAX_CHARS']='16000'
    raw=(args.data/'sqlite.jsonl').read_text()
    cases=json.loads(raw) if raw.lstrip().startswith('[') else [json.loads(x) for x in raw.splitlines()]
    if len(cases)!=500 or len({c['question_id'] for c in cases})!=500:
        raise ValueError('Expected all 500 unique BIRD Mini-Dev questions')
    if args.profile == 'xiyan' and args.model != MODELS['xiyan']:
        ap.error('XiYan profile requires the matched XiYan 7B model')
    primary_tokens = 2048 if args.profile in {'xiyan','generic'} else 512
    root=args.data/'databases_full'
    base_manifest = None
    if args.baseline is not None:
        base_manifest = json.loads((args.baseline/'manifest.json').read_text())
        if (base_manifest['data_sha256'] != hashlib.sha256(raw.encode()).hexdigest()
                or (not args.comparison_reference and base_manifest['model'] != args.model)):
            raise ValueError('Baseline dataset or model differs')
    with urllib.request.urlopen('http://127.0.0.1:11434/api/tags',timeout=15) as resp:
        installed={m['name']:m['digest'] for m in json.load(resp)['models']}
    for name in (args.model,args.reviewer):
        if name not in installed:
            raise ValueError('Missing model; run: ollama pull '+name)
    if not args.comparison_reference and installed[args.model]!=base_manifest['model_details'][0]['digest']:
        raise ValueError('Baseline model weights differ')
    policies={}; hashes={}
    for did in sorted({c['db_id'] for c in cases}):
        path=root/did/(did+'.sqlite')
        h=hashlib.sha256()
        with path.open('rb') as f:
            for block in iter(lambda:f.read(4*1024*1024),b''): h.update(block)
        hashes[did]=h.hexdigest()
        tables=tuple(r[0] for r in execute(path,"SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))
        policies[did]=MutationPolicy(path,tables,max_rows=100000,timeout_seconds=30,max_database_bytes=path.stat().st_size+1)
    if base_manifest is not None and hashes != base_manifest['database_hashes']:
        raise ValueError('Baseline database contents differ')
    # Fail before inference if execution limits cannot be represented by the verifier.
    from sql_agent.data_agent import DataContract
    for policy in policies.values():
        DataContract((), dynamic_columns=True, max_rows=min(policy.max_rows, 10000))
    docs=build_documents(root,policies)
    field_metadata = None
    if args.variant.startswith('field_'):
        from sql_agent.grounded_retrieval import field_documents
        docs, field_metadata = field_documents(root, policies)
    source=Path(__file__).resolve().parents[2]/'src/sql_agent'
    manifest=dict(semantic_repair=args.semantic_repair,execution_max_rows=100000,execution_timeout_seconds=30,variant=args.variant,model=args.model,reviewer=args.reviewer,model_digests={n:installed[n] for n in (args.model,args.reviewer)},
        database_hashes=hashes,data_sha256=base_manifest['data_sha256'],retrieval=args.retrieval,
        embedding_path=str(args.embedding_model),reranker_path=str(args.reranker_model),
        embedding_files=model_files(args.embedding_model),reranker_files=model_files(args.reranker_model),
        corpus_sha256=hashlib.sha256(json.dumps(docs,sort_keys=True).encode()).hexdigest(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        production_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.glob('*.py')},
        scope='Production DatabaseWorkflow with '+args.profile+' text-output planner, public CSV RAG, schema linking, production query ACL, up to 3 proposals, independent advisory verifier. No writes; no gold in runtime.',
        options=dict(temperature=0,seed=917,num_predict=primary_tokens,num_ctx=16384),reviewer_max_tokens=512,
        profile_sha256=hashlib.sha256(Path(__file__).with_name("sql_model_profiles.py").read_bytes()).hexdigest(),schema_max_tables=8,schema_max_chars=16000)
    manifest['pv_verification'] = args.pv_verification
    manifest['disable_thinking'] = args.disable_thinking
    manifest['planner_output'] = 'json_sql' if args.profile == 'generic' else 'text'
    manifest['qwen_non_thinking_transport'] = 'raw_closed_think' if args.disable_thinking and args.model.startswith('qwen3:') else None
    manifest['comparison'] = (dict(
        kind='cross_model_reference' if args.comparison_reference else 'matched_model_baseline',
        model=base_manifest['model'], path=str(args.baseline))
        if base_manifest is not None else {'kind': 'none'})
    args.output.mkdir(parents=True,exist_ok=args.resume)
    if args.resume: configuration_matches(json.loads((args.output/'manifest.json').read_text()),manifest)
    else: save_json(args.output/'manifest.json',manifest)
    service=MutationService(args.output/'control.sqlite',policies)
    retriever=KnowledgeRetriever(docs if args.variant != 'full_no_rag' else [],mode=args.retrieval if args.variant != 'full_no_rag' else 'bm25',model_path=args.embedding_model,
        cache_path=args.output/'embeddings.sqlite' if args.retrieval=='hybrid' else None,reranker_path=args.reranker_model if args.variant != 'full_no_rag' else None)
    if field_metadata is not None:
        from sql_agent.grounded_retrieval import GroundedRetriever
        retriever=GroundedRetriever(field_metadata,policies,mode=args.variant,
            model_path=args.embedding_model,cache_path=args.output/'embeddings.sqlite',reranker_path=args.reranker_model)
    # Fail before 500 episodes if local embedding/reranker dependencies are missing.
    retriever.retrieve(cases[0]['db_id'],cases[0]['question'],policies[cases[0]['db_id']].tables)
    baseline = ({str(c['question_id']): json.loads(
        (args.baseline/f"{c['question_id']}.json").read_text()) for c in cases}
        if args.baseline is not None else {})
    records=[]
    for case in cases:
        target=args.output/f"{case['question_id']}.json"
        if args.resume and target.exists():
            saved=json.loads(target.read_text())
            if saved['question_id']!=case['question_id'] or saved['db_id']!=case['db_id']: raise ValueError('Record identity mismatch')
            records.append(saved); continue
        primary=LoggedModel(args.model,max_tokens=primary_tokens,disable_thinking=args.disable_thinking,
                            sql_only=args.profile == 'generic')
        reviewer=LoggedModel(args.reviewer,structured=True)
        planner=BirdPlanner(service,primary,reviewer,retriever,profile=args.profile,
            schema_mode='selected' if args.variant=='selected_rag' else 'full',
            use_rag=args.variant!='full_no_rag', evidence=case.get('evidence',''), original_question=case['question'])
        planner.semantic_repair_enabled = args.semantic_repair
        planner.pv_verification_enabled = args.pv_verification
        graph=DatabaseWorkflow(service).graph(MemorySaver(),planner=planner)
        config={'configurable':{'thread_id':str(case['question_id'])}}
        question=case['question']+'\nProvided BIRD evidence: '+case.get('evidence','')
        record=dict(question_id=case['question_id'],db_id=case['db_id'],correct=False,status='failed')
        start=time.monotonic(); state={}
        try:
            state=graph.invoke(dict(id=str(case['question_id']),database_id=case['db_id'],question=question,
                sql='',mode='auto',allow_writes=False,submitted_by='benchmark'),config)
            result=state.get('result')
            if result and not result.get('truncated'):
                expected=execute(policies[case['db_id']].database,case['SQL'])
                record['correct']=set(map(tuple,result['rows']))==set(expected)
                record['status']='correct' if record['correct'] else 'wrong'
            else: record['status']='clarification' if state.get('clarification') else 'no_result'
        except Exception as exc:
            record['error']=str(exc)
            state=graph.get_state(config).values
        record.update(state=state,review=state.get('semantic_review',{}),primary_calls=primary.calls,reviewer_calls=reviewer.calls,
                      elapsed_seconds=time.monotonic()-start)
        record['tokens']=sum((c.get('response',{}).get('prompt_eval_count',0)+c.get('response',{}).get('eval_count',0)) for c in primary.calls+reviewer.calls)
        save_json(target,record); records.append(record)
        save_json(args.output/'summary.json',summarize(records,baseline))
        recent = records[-5:]
        if len(recent) == 5 and all(r.get('error') in {
                'truncated SQL generation', 'No unambiguous final SQL block or plain SQL response'
                } or str(r.get('error', '')).startswith('HTTP Error') for r in recent):
            raise RuntimeError('Five consecutive model/format failures; stopping to avoid an invalid full run')
        if len(records)%10==0: print(f"{len(records)}/500: {sum(r['correct'] for r in records)} correct",flush=True)
    summary=summarize(records,baseline)
    summary['comparison'] = manifest['comparison']
    save_json(args.output/'summary.json',summary)
    print(f"Finished: {summary['correct']}/500 correct. Results: {args.output}",flush=True)

if __name__=='__main__': main()
