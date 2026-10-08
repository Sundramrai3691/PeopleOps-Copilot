"""One portfolio page for full development and new-schema model comparisons."""
import argparse
from collections import Counter
import hashlib
import html
import json
import math
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
RUNS=('native_qwen8_v1','native_omnisql_v1','native_coder14_v1','billing_transfer_v1','guided_coder14_dev_v2','guided_coder14_billing_v2','manufacturing_transfer_v2','quality_regression_v1','fast_sql_regression_v1')


def load_run(name):
    path=ROOT/'outputs/validation'/name
    manifest=json.loads((path/'manifest.json').read_text())
    summary=json.loads((path/'summary.json').read_text())
    rows=[json.loads(p.read_text()) for p in sorted(path.glob('episode_*.json'))]
    if len(rows)!=manifest['episodes_planned'] or len(rows)!=summary['episodes']:
        raise ValueError('incomplete run: '+name)
    if name in ('manufacturing_transfer_v2','quality_regression_v1','fast_sql_regression_v1'):
        keys=[(r['case_id'],r['variant'],r['trial'],r['method']) for r in rows]
        planned=[tuple(s) for s in manifest['schedule']]
        if summary.get('model_unchanged') is not True:raise ValueError('model digest changed')
    elif name=='billing_transfer_v1':
        keys=[(r['model'],r['case_id'],r['variant'],r['trial']) for r in rows]
        planned=[tuple(s) for s in manifest['schedule']]
        if summary.get('models_unchanged') is not True:raise ValueError('model digest changed')
    else:
        keys=[(r['case_id'],r['variant'],r['method'],r['profile'],r['trial']) for r in rows]
        planned=[tuple(s[:5]) for s in manifest['schedule']]
    if len(keys)!=len(set(keys)) or Counter(keys)!=Counter(planned):
        raise ValueError('schedule mismatch: '+name)
    if not summary['sources_unchanged'] or not summary['databases_unchanged']:
        raise ValueError('integrity mismatch: '+name)
    for relative,digest in manifest['source_sha256'].items():
        if hashlib.sha256((path/'source_snapshot'/relative).read_bytes()).hexdigest()!=digest:
            raise ValueError('source snapshot mismatch: '+name)
    for relative,digest in manifest['database_sha256'].items():
        if hashlib.sha256((path/relative).read_bytes()).hexdigest()!=digest:
            raise ValueError('database snapshot mismatch: '+name)
    if not all(r['source_unchanged'] for r in rows):raise ValueError('episode source changed')
    return manifest,summary,rows



def render_robustness():
    path=ROOT/'outputs/validation/billing_robustness_v1'
    if not path.exists():return ''
    summary=json.loads((path/'summary.json').read_text())
    manifest=json.loads((path/'manifest.json').read_text())
    assert all(summary[k] for k in ('sources_unchanged','input_episodes_unchanged','databases_unchanged'))
    for name,digest in manifest['database_sha256'].items():
        assert hashlib.sha256((path/name).read_bytes()).hexdigest()==digest
    for name,digest in manifest['source_sha256'].items():
        assert hashlib.sha256((path/'source_snapshot'/name).read_bytes()).hexdigest()==digest
    for name,digest in manifest['source_episode_sha256'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest
    parts=['<h2>Data robustness: 24 additional instances beyond the original two</h2>',
           '<p>Eight known billing questions; 48 original records per configuration, with stopped runs counted as failures. No additional model calls. Strict success requires the same SQL to pass both original and all 24 additional instances.</p>',
           '<table><tr><th>Configuration</th><th>Correct on both original instances</th><th>Correct on all 26 instances</th><th>Newly detected failures</th></tr>']
    labels={'guided_coder14_billing_v2':'Coder 14B / One round','quality_regression_v1':'Qwen3 14B / Reasoning and repair','fast_sql_regression_v1':'Qwen3 14B / Non-reasoning and repair'}
    for name,stat in summary['by_configuration'].items():
        rows=json.loads((path/(name+'.json')).read_text())
        assert len(rows)==stat['saved_candidates']==48
        assert all(len(r['checks'])==24 and {c['instance'] for c in r['checks']}==set(range(24)) for r in rows)
        assert sum(r['original_both_instances_correct'] and all(c['correct'] for c in r['checks']) for r in rows)==stat['all_26_instances_correct']
        parts.append('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in [labels[name],
            str(stat['original_both_instances_correct'])+'/48',str(stat['all_26_instances_correct'])+'/48',
            stat['lost_after_original_both_passed']])+'</tr>')
    parts.append('</table><p>Includes empty child tables, nullable join keys, equal-valued records, and fractional percentages. These are additional local data stress checks, not a public benchmark or production certification.</p>')
    return ''.join(parts)


def render():
    body=['<h1>SQL-Agent: Model selection and transfer evaluation</h1>',
          '<p>Historical SQL-text comparisons: 2048 tokens and one SQL round. Two newer Qwen3 14B profiles: 8192 tokens and up to three SQL rounds, with reasoning enabled or disabled. No additional probing or model checker. '
          'All general results still require review; these are not production accuracy claims.</p>']
    loaded={name:load_run(name) for name in RUNS}
    if all(n in loaded for n in ('quality_regression_v1','fast_sql_regression_v1')):
        baseline=[r for name in ('guided_coder14_dev_v2','guided_coder14_billing_v2','manufacturing_transfer_v2')
                  for r in loaded[name][2] if r['method']=='native_sql_guided']
        current=[('Coder 14B / One round',baseline),('Qwen3 14B / Reasoning and repair',loaded['quality_regression_v1'][2]),
                 ('Qwen3 14B / Non-reasoning and repair',loaded['fast_sql_regression_v1'][2])]
        body.append('<h2>Configuration comparison on the same 26 known questions</h2><p>Two instances and three trials per question: 156 episodes per configuration, not 156 distinct questions. Retention refers to candidates; general answers are not released automatically.</p>')
        body.append('<table><tr><th>Configuration</th><th>Correct candidates</th><th>Incorrect candidates</th><th>Stopped</th><th>Calls</th><th>p95 seconds</th></tr>')
        for label,items in current:
            times=sorted(r['elapsed_ms'] for r in items)
            cells=[label,str(sum(r['accepted_correct'] for r in items))+'/'+str(len(items)),
                   sum(r['accepted_incorrect'] for r in items),sum(not r['controller_accepted'] for r in items),
                   sum(len(r['calls']) for r in items),f'{times[math.ceil(.95*len(times))-1]/1000:.2f}']
            body.append('<tr>'+''.join('<td>'+html.escape(str(c))+'</td>' for c in cells)+'</tr>')
        body.append('</table><p>Multiple factors change between configurations. Accuracy differences cannot be attributed to reasoning alone; latency was measured sequentially on local hardware.</p>')
    body.append(render_robustness())
    total=0
    for name in RUNS:
        manifest,summary,rows=loaded[name];total+=len(rows)
        body.append('<h2>'+html.escape(name)+'</h2>')
        scope=('Reasoning and repair regression on all 26 known questions; not an unseen test set.' if name in ('quality_regression_v1','fast_sql_regression_v1') else 'New manufacturing schema: paired evaluation after freezing the prompt; operators overlap with development tasks.' if name=='manufacturing_transfer_v2' else 'Billing tasks: v1 was the initial transfer evaluation; v2 is development regression.' if 'billing' in name else 'Inventory, support, and shipping tasks already used during development.')
        body.append('<p>'+scope+'</p>')
        groups=[]
        if name in ('quality_regression_v1','fast_sql_regression_v1'):
            profile='thinking + repair' if manifest['thinking'] else 'greedy + repair'
            groups=[(manifest['model']['name']+' / '+profile,summary['all'],rows)]
        elif 'by_model' in summary:
            groups=[(model,stat,[r for r in rows if r['model']==model]) for model,stat in summary['by_model'].items()]
        else:
            model=manifest['model'][0]['name'] if isinstance(manifest['model'],list) else manifest['model']['name']
            groups=[(model+' / '+method,stat.get('all',stat),[r for r in rows if r['method']==method]) for method,stat in summary['by_method'].items()]
        body.append('<table><tr><th>Model</th><th>Correct retained</th><th>Incorrect retained</th><th>Stopped</th><th>Model calls</th><th>p95 seconds</th></tr>')
        for model,s,group in groups:
            if (sum(r['accepted_correct'] for r in group),sum(r['accepted_incorrect'] for r in group))!=(s['accepted_correct'],s['accepted_incorrect']):
                raise ValueError('summary count mismatch')
            body.append(f'<tr><td>{html.escape(model)}</td><td>{s["accepted_correct"]}/{s["episodes"]}</td>'
                        f'<td>{s["accepted_incorrect"]}</td><td>{s["held_or_stopped"]}</td>'
                        f'<td>{s["real_provider_requests"]}</td><td>{s["pipeline_p95_ms"]/1000:.2f}</td></tr>')
        body.append('</table><details><summary>Expand all episodes and SQL, including failures</summary>')
        for row in rows:
            title=f'{row["case_id"]} · variant {row["variant"]} · trial {row["trial"]} · {row.get("model","")} · correct={row["accepted_correct"]}'
            evidence={k:row[k] for k in ('first_query_test_suite','source_unchanged','auto_released')}
            evidence['trajectory']=[t for t in row['pipeline']['result']['agent_trajectory'] if t['step'] in ('propose','route')]
            body.append('<details class="episode"><summary>'+html.escape(title)+'</summary><pre>'
                        +html.escape(json.dumps(evidence,indent=2,ensure_ascii=False))+'</pre></details>')
        body.append('</details>')
    body.append(f'<p id="total">All {total} episodes. Models ran in sequential blocks, without randomized latency interleaving; model size and quantization also differ. '
                'The original JSON baseline of 39/72 used a different protocol from the SQL-text runs here; not all changes can be attributed to the model.</p>')
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><title>SQL-Agent Model Evidence</title>'
            '<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:20px;color:#183343}'
            'table{width:100%;border-collapse:collapse}td,th{padding:12px;border-bottom:1px solid #ddd;text-align:left}'
            'details{margin:10px 0;padding:10px;background:#f3f6f8}summary{cursor:pointer}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'+''.join(body)+'</html>')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();page=render()
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(page)
