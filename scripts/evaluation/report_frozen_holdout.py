"""Verify a complete, unchanged single-run holdout and report explicit rates."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from scripts.evaluation.holdout_metrics import metrics


def read(path):return json.loads(path.read_text())
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def rate(value):return 'undefined' if value['rate'] is None else f"{100*value['rate']:.1f}% ({value['numerator']}/{value['denominator']})"


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--markdown',type=Path,required=True);args=p.parse_args()
    root=args.root;dataset=root/'dataset';trial=root/'live/trial';freeze=read(root/'freeze.json');manifest=read(trial/'manifest.json')
    for name,digest in freeze['source_sha256'].items():
        if sha(Path(name))!=digest or manifest['source_sha256'].get(name)!=digest:raise ValueError('Runtime changed after freeze: '+name)
    if manifest['image_id']!=freeze['image_id'] or manifest['task_warmup']:raise ValueError('Frozen image or single-run protocol differs')
    if manifest['model_metadata']!=read(root/'model_identity_verification.json')['model_metadata']:
        raise ValueError('Model weights differ from the frozen-version reference')
    for name,digest in read(dataset/'sealed_inputs.json').items():
        if sha(dataset/name)!=digest:raise ValueError('Sealed dataset changed')
    if sha(dataset/'evaluation.json')!=manifest['dataset_sha256'] or sha(dataset/'knowledge.json')!=manifest['knowledge_sha256']:raise ValueError('Wrong evaluated dataset')
    inference=read(root/'live/inference.json');evidence=read(trial/'bm25/evidence.json')
    if not all([inference['trial_exit_code']==0,inference['temporary_inference_stopped'],inference['original_model_manifests_unchanged'],evidence['run_complete'],evidence['business_unchanged'],evidence['containers_removed']]):raise ValueError('Run or cleanup incomplete')
    paths=sorted((trial/'bm25').glob('eval-*.json'));rows=[read(path) for path in paths]
    cases={c['case_id']:c for c in read(dataset/'evaluation.json')['cases']}
    if len(rows)!=len(cases) or {r['case_id'] for r in rows}!=cases.keys():raise ValueError('Missing or duplicate cases')
    if list((trial/'bm25').glob('warm-*.json')):raise ValueError('Holdout tasks repeated as warmup')
    counts=Counter(c['stratum'] for c in cases.values())
    if dict(counts)!=freeze['strata']:raise ValueError('Stratum quotas differ')
    wrong_agreements=[]
    for r in rows:
        case=cases[r['case_id']];r.update(stratum=case['stratum'],domain=case['domain'])
        if r['family']!=case['family']:raise ValueError('Cluster label changed')
        result=(r.get('job') or {}).get('result') or {}
        candidate=result.get('candidate_output') or result.get('output')
        verified=bool(candidate and candidate.get('rows')==case['expected']['rows'] and candidate.get('columns')==case['expected']['columns'])
        if verified!=r['correct']:raise ValueError('Correctness differs from external oracle')
        if r['verifier_status']=='agreement' and not verified:
            wrong_agreements.append({'case_id':r['case_id'],'stratum':r['stratum'],'question':case['question'],
                'failure_kind':'wrong_rows' if candidate and candidate.get('rows')!=case['expected']['rows'] else 'output_column_contract',
                'planner_sql':result.get('sql'),'verifier_sql':result.get('semantic_review',{}).get('sql'),
                'candidate':candidate,'expected':case['expected']})
    summaries={name:metrics([r for r in rows if r['stratum']==name]) for name in freeze['strata']}
    families={name:metrics([r for r in rows if r['family']==name]) for name in sorted({r['family'] for r in rows})}
    domains={name:metrics([r for r in rows if r['domain']==name]) for name in sorted({r['domain'] for r in rows})}
    report={'scope':freeze['scope'],'freeze_sha256':sha(root/'freeze.json'),'dataset_sha256':sha(dataset/'evaluation.json'),
        'runtime_unchanged':True,'single_run':True,'strata':summaries,'families':families,'domains':domains,
        'pooled_descriptive_only':metrics(rows),'wrong_agreements':wrong_agreements,
        'wrong_agreement_kinds':dict(Counter(r['failure_kind'] for r in wrong_agreements)),
        'raw_sha256':{p.name:sha(p) for p in paths},
        'limitations':['Only 3 blind domains and 15 blind templates; four variants per template are dependent.',
          'Operator-authored synthetic test, not external human-adjudicated workload.',
          'Correlated-risk cases were selected for known failure mechanisms, not observed new-run failures; selection cannot estimate natural-traffic correlation.',
          'No runtime or hyperparameter change after freeze. Once inspected these cases are spent holdout.',
          'Latency includes cold start; earlier 72.9% development comparison excluded task warmups. Different populations are not paired.',
          'Planner and Verifier share schema and retrieved context; model agreement is not semantic proof.'],
        'production_approval':False}
    with (root/'report.json').open('x') as f:json.dump(report,f,indent=2)
    lines=['# Frozen Agent holdout and challenge evaluation','',freeze['scope']+'.','',
        'Runtime, model identities and prompt were frozen before dataset authoring. Each of 108 requests ran once with BM25, qwen3:8b Planner and qwen2.5-coder:14b Verifier. Expected rows and reference SQL were not mounted into the runtime.',
        'All 108 Python oracles were cross-checked against separately written SQL before inference. No result-driven model/prompt changes were made.', '',
        '| Set | Task accuracy | Verifier false accept | Human review | Tokens / correct | p95 seconds |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for name,m in summaries.items():
        cost=m['token_cost']['tokens_per_correct_result'];cost='unknown/undefined' if cost is None else f'{cost:,.1f}'
        lines.append(f"| {name} | {rate(m['task_accuracy'])} | {rate(m['verifier_false_accept_rate'])} | {rate(m['review'])} | {cost} | {m['p95_seconds']:.2f} |")
    lines+=['','## Acceptance denominators','',
        'Verifier false accept = wrong candidates with agreement / all wrong candidates. Also report the fraction of agreements that are wrong (false discovery). System false accept counts actual completed releases, separately from model agreement. Zero releases do not establish verifier reliability. Undefined denominators are not zero.', '',
        '| Set | Wrong / agreements | Wrong system releases / wrong candidates | Task-accuracy cluster 95% interval |',
        '| --- | ---: | ---: | ---: |']
    for name,m in summaries.items():
        lo,hi=m['accuracy_cluster95']
        lines.append(f"| {name} | {rate(m['verifier_false_discovery_rate'])} | {rate(m['system_false_accept_rate'])} | {100*lo:.1f}%–{100*hi:.1f}% |")
    lines+=['','## Template breakdown','','| Domain / template | Correct | Wrong agreements |','| --- | ---: | ---: |']
    for name,m in families.items():lines.append(f"| {name} | {m['correct']}/{m['n']} | {m['verifier_false_accept_rate']['numerator']} |")
    lines+=['','## Interpretation and limits','',
        'The earlier 35/48 (72.9%) is a seen development-set reference, not a paired baseline for these new tasks. Comparable point estimates alone do not prove distribution-independent accuracy.',
        'Confidence intervals resample whole question templates; only three blind domains limits domain-level inference. Challenge strata are reported separately rather than pooled into a generalization headline.',
        'Every wrong answer, failure and retry remains in workload token cost. A correct task means external-oracle agreement, not successful SQL execution or verifier agreement.',
        'Human review is the terminal needs_review fraction; pending review is not an automatically delivered correct answer. Latency is client submission to terminal observation, includes cold start and failures, and is not a production SLO.',
        'Known-risk selection measures joint wrong agreement on those mechanisms; it does not estimate a model-error correlation coefficient or independence.',
        'The author/evaluator could access the synthetic answer definitions. This is model-blind, post-freeze evidence, not an independently sealed third-party test. After this report the set must not be reused as a fresh blind holdout.', '',
        'Artifacts: `outputs/validation/frozen-holdout-v1/` contains frozen source, hashes, dataset, oracle checks, per-request responses, metrics and wrong-agreement SQL pairs. Credentials remain local and are not copied into this report.', '']
    args.markdown.write_text('\n'.join(lines))
    print(json.dumps({name:{k:m[k] for k in ('task_accuracy','verifier_false_accept_rate','verifier_false_discovery_rate','review','token_cost','p95_seconds','accuracy_cluster95')} for name,m in summaries.items()},indent=2))


if __name__=='__main__':main()
