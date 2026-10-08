"""Render a completed benchmark's recorded results; never generate missing scores."""
import argparse
from collections import Counter
from html import escape
import json
from pathlib import Path


NAMES = {'one_shot': 'One-shot', 'execution_retry': 'Execution retry', 'checked_agent': 'Independent checker'}


def load_complete(directory):
    manifest = json.loads((directory/'manifest.json').read_text())
    summary = json.loads((directory/'summary.json').read_text())
    rows = [json.loads(p.read_text()) for p in sorted(directory.glob('episode_*.json'))]
    expected = {(c,v,m,p,t) for c,v,m,p,t,f in manifest['schedule']}
    actual = [(r['case_id'],r['variant'],r['method'],r['profile'],r['trial']) for r in rows]
    if (len(actual) != len(set(actual)) or set(actual) != expected or len(rows) != summary['episodes']
            or len(rows) != manifest['episodes_planned']):
        raise ValueError('Incomplete, duplicate or unexpected episodes; refusing to render a final report')
    for method in NAMES:
        for profile in ('clean', 'transient'):
            group = [r for r in rows if r['method']==method and r['profile']==profile]
            totals = summary['by_method'][method][profile]
            if totals['episodes'] != len(group) or totals['accepted_correct'] != sum(r['accepted_correct'] for r in group):
                raise ValueError('Summary and raw episodes disagree')
    return manifest, summary, rows


def percent(value):
    return 'N/A' if value is None else f'{value*100:.1f}%'


def make_report(directory):
    manifest, summary, rows = load_complete(directory)
    families, pairs, repeats = summary['distinct_questions'], summary['question_database_pairs'], summary['repeats']
    markdown = ['# Paired SQL benchmark results', '',
                f"{summary['episodes']} episodes · {families} question families · {pairs} question/database pairs · {repeats} full trials.", '',
                '**Scope:** frozen synthetic evaluation; no runtime answers or catalog fallback. '
                'Controller acceptance is not automatic publication. Generic results remain under review.', '']
    blocks = []
    for profile in ('clean','transient'):
        title = 'Clean execution' if profile == 'clean' else 'One controlled timeout / HTTP 429'
        markdown += [f'## {title}', '', '| Method | Correct retained | Wrong retained | Held/stopped | All repeats successful | Attempts | p95 ms |',
                     '|---|---:|---:|---:|---:|---:|---:|']
        html_rows, bars = [], []
        for method, name in NAMES.items():
            m = summary['by_method'][method][profile]
            n = m['episodes']
            correct, wrong, held = m['accepted_correct'], m['accepted_incorrect'], m['held_or_stopped']
            all_k = percent(m['observed_all_repeats_success_rate'])
            markdown.append(f"| {name} | {correct}/{n} | {wrong} | {held} | {all_k} | {m['model_attempts_including_injected_faults']} | {m['pipeline_p95_ms']:.1f} |")
            cells = [name, f'{correct}/{n}', str(wrong), str(held), all_k,
                     str(m['model_attempts_including_injected_faults']), f"{m['pipeline_p95_ms']:.1f}"]
            html_rows.append('<tr>'+''.join('<td>'+escape(c)+'</td>' for c in cells)+'</tr>')
            bars.append(f'<div class="barlabel">{escape(name)} — {correct} correct, {wrong} wrong, {held} held</div>'
                        f'<div class="bar" aria-label="{correct} correct of {n}">'
                        f'<span class="correct" style="width:{correct/n*100}%"></span>'
                        f'<span class="wrong" style="width:{wrong/n*100}%"></span>'
                        f'<span class="held" style="width:{held/n*100}%"></span></div>')
        markdown.append('')
        blocks.append(f'<section><h2>{title}</h2>'+''.join(bars)+
                      '<div class="scroll"><table><tr><th>Method</th><th>Correct retained</th><th>Wrong retained</th>'
                      f'<th>Held / stopped</th><th>All {repeats} trials successful</th><th>Attempts</th><th>p95 ms</th></tr>'+
                      ''.join(html_rows)+'</table></div></section>')
    markdown += ['## Paired comparisons (clean)', '']
    comparisons = []
    for comparison in summary['paired_clean_differences']:
        lo, hi = comparison['cluster_bootstrap_95_percentile_interval']
        description = (f"{NAMES[comparison['left']]} minus {NAMES[comparison['right']]}: "
                       f"{comparison['mean_difference']*100:+.1f} percentage points; "
                       f"question-cluster bootstrap interval [{lo*100:+.1f}, {hi*100:+.1f}].")
        markdown.append('- '+description)
        comparisons.append('<p>'+escape(description)+'</p>')
    blocks.append('<section><h2>Paired differences</h2>'+''.join(comparisons)+
                  f'<p class="note">{families} synthetic question families; this is descriptive uncertainty, not a population guarantee.</p></section>')
    markdown += ['', '## Costs, cross-instance checks and false rejection', '',
                 '| Method | Correct on both instances, retained | Correct candidates held | Observed input/output tokens | Missing usage attempts |',
                 '|---|---:|---:|---:|---:|']
    cost_rows = []
    for method, name in NAMES.items():
        m = summary['by_method'][method]['all']
        markdown.append(f"| {name} | {m['same_query_correct_on_both_instances']}/{m['episodes']} | {m['correct_candidate_held']} | {m['prompt_tokens_observed']}/{m['completion_tokens_observed']} | {m['calls_without_complete_usage']} |")
        values = [name, f"{m['same_query_correct_on_both_instances']}/{m['episodes']}",
                  str(m['correct_candidate_held']), f"{m['prompt_tokens_observed']}/{m['completion_tokens_observed']}",
                  str(m['calls_without_complete_usage'])]
        cost_rows.append('<tr>'+''.join('<td>'+escape(v)+'</td>' for v in values)+'</tr>')
    blocks.append('<section><h2>Cross-instance checks and model usage</h2><div class="scroll"><table><tr><th>Method</th>'
                  '<th>Correct on both instances, retained</th><th>Correct candidates held</th><th>Input / output tokens</th><th>Missing usage attempts</th></tr>'+
                  ''.join(cost_rows)+'</table></div><p class="note">Includes both profiles and all failed episodes. Dollar cost is unknown.</p></section>')
    details = []
    for case in manifest['cases']:
        selected = [r for r in rows if r['case_id'] == case['case_id'] and r['profile'] == 'clean']
        lines = []
        for method, name in NAMES.items():
            group = [r for r in selected if r['method'] == method]
            reasons = Counter(r['pipeline']['result']['routing']['reason'] for r in group if not r['accepted_correct'])
            lines.append(f"{name}: {sum(r['accepted_correct'] for r in group)}/{len(group)} correct retained; failure reasons {dict(reasons)}")
        details.append('<details><summary>'+escape(case['case_id'])+'</summary><p>'+escape(case['question'])+'</p><pre>'+escape('\n'.join(lines))+'</pre></details>')
    blocks.append('<section><h2>Inspect each question family</h2>'+''.join(details)+'</section>')
    notes = [
        'Automatic publication is disabled for all three methods; zero releases is a policy, not proof of useful correctness.',
        f'The two database instances and repeated trials are correlated. These are {families} question families, not {len(rows)} unique questions.',
        f"Each method has {summary['by_method']['one_shot']['clean']['episodes']} clean episodes and {summary['by_method']['one_shot']['transient']['episodes']} transient episodes. Faults are simulated without network wait.",
        'All methods share a nine-attempt ceiling. One-shot has one SQL planning round, other methods up to three; actual usage differs.',
        'Costs include failed episodes. Tokens are observed provider usage; injected failures have no usage. Dollar cost is unknown.',
        'Pipeline timing excludes queue, HTTP and offline grading. This is not a concurrent-load or production SLO measurement.',
        'This run is a fixed evaluation of existing policies. Subsequent use of these cases for tuning makes them development data.',
        f"Integrity: sources unchanged at run end={summary['sources_unchanged']}; databases unchanged={summary['databases_unchanged']}; all within attempt budget={summary['all_within_attempt_budget']}.",
    ]
    markdown += ['', '## Interpretation limits', ''] + ['- '+n for n in notes]
    blocks.append('<section><h2>Evidence boundaries</h2><ul>'+''.join('<li>'+escape(n)+'</li>' for n in notes)+'</ul></section>')
    html = ('''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SQL-Agent | Benchmark evidence</title><style>
body{font:15px system-ui;max-width:1120px;margin:36px auto;padding:0 20px;color:#183441;background:#f2f6f8}
h1{font-size:30px}h2{font-size:20px}p,li{line-height:1.6}section{background:white;padding:24px;border:1px solid #d7e1e7;border-radius:12px;margin:20px 0}
.note{color:#536873}.warning{border-left:4px solid #b46c21;padding:12px;background:#fff4df}.bar{display:flex;height:18px;border-radius:4px;overflow:hidden;background:#dce4e8;margin:6px 0 18px}.correct{background:#147c82}.wrong{background:#c44940}.held{background:#dce4e8}
.barlabel{font-weight:600}table{border-collapse:collapse;width:100%;margin-top:22px;font-size:14px}th,td{text-align:left;padding:10px;border-bottom:1px solid #dbe4e8}.scroll{overflow:auto}details{padding:12px 0;border-bottom:1px solid #e0e8ec}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}a{color:#12636e}
</style><h1>SQL-Agent · Benchmark evidence</h1>'''+f'<p>{len(rows)} episodes · {families} question families · {pairs} question/database pairs · {repeats} full trials</p>'+
        '<p class="warning">Synthetic evaluation. Correct retained candidates are not automatically published results. No production accuracy or SLO claim.</p>'+
        '<p class="note">Teal: correct retained · Red: wrong retained · Gray: held or stopped</p>'+''.join(blocks)+'</html>')
    (directory/'report.md').write_text('\n'.join(markdown)+'\n')
    (directory/'report.html').write_text(html)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    make_report(parser.parse_args().directory)
