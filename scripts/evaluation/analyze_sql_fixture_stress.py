"""Post-hoc fixture stress of saved SQL, not a new held-out model evaluation.

Duplicate source INSERT rows and re-execute source SQL and both saved proposals.
This exposes accidental agreement such as SUM versus AVG on one row. Constraints
may reject duplicates; those cases are unscorable, never silently called correct.
"""
import argparse
import hashlib
import json
from pathlib import Path

from sqlglot import exp, parse
from sql_agent.acceptance_report import paired_comparison
from sql_agent.training_dataset import execute_read, score_response


def stress_case(row, responses):
    statements = parse(row['sql_context'], read='sqlite')
    inserts = [node.sql(dialect='sqlite') for node in statements if isinstance(node, exp.Insert)]
    record = {'case_id':str(row['id']), 'family':row['domain'], 'scorable':False}
    try:
        if not inserts:
            raise ValueError('no source INSERT rows')
        context = row['sql_context'].rstrip().rstrip(';')+'; '+'; '.join(inserts)+';'
        expected = execute_read(context, row['sql'])
        modified = {**row, 'sql_context':context, 'expected_rows':expected}
        record.update(scorable=True, fixture_sha256=hashlib.sha256(context.encode()).hexdigest())
        record['conditions'] = {}
        for name, response in responses.items():
            original = score_response(row, response)
            stress = score_response(modified, response)
            record['conditions'][name] = {'original':original, 'duplicated_fixture':stress,
                'both_match_source_sql':original['correct'] and stress['correct']}
    except ValueError as exc:
        record.update(reason=str(exc))
    return record


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,required=True)
    p.add_argument('--run',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args = p.parse_args()
    data = json.loads(args.dataset.read_text())
    manifest = json.loads((args.run/'manifest.json').read_text())
    if hashlib.sha256(args.dataset.read_bytes()).hexdigest() != manifest['dataset_sha256']:
        raise ValueError('training evaluation dataset changed')
    ids = {row['id'] for row in data['splits']['test']}
    responses = {}
    hashes = {}
    for name in ('baseline','adapter'):
        path = args.run/(name+'.json')
        raw = json.loads(path.read_text())
        responses[name] = {r['id']:r['response'] for r in raw}
        if set(responses[name]) != ids or len(raw) != len(ids):
            raise ValueError('incomplete or duplicate generation IDs')
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    rows = [stress_case(row, {name:values[row['id']] for name,values in responses.items()})
            for row in data['splits']['test']]
    paired = {}
    counts = {}
    for name in responses:
        paired[name] = [{'case_id':r['case_id'],'family':r['family'],
            'correct':r['conditions'][name]['both_match_source_sql']} for r in rows if r['scorable']]
        counts[name] = {'both_fixtures_match':sum(r['correct'] for r in paired[name]),
            'original_matches_lost':sum(r['scorable'] and r['conditions'][name]['original']['correct']
                and not r['conditions'][name]['duplicated_fixture']['correct'] for r in rows)}
    result = {'scope':__doc__, 'planned':len(rows),'scorable':len(paired['baseline']),
        'unscorable':sum(not r['scorable'] for r in rows),'raw_sha256':hashes,
        'dataset_sha256':manifest['dataset_sha256'],'counts':counts,
        'paired':paired_comparison(paired['baseline'],paired['adapter']), 'records':rows,
        'promotion':'No promotion. Source labels and fixture stress are not human business validation.'}
    with args.output.open('x') as f:
        json.dump(result,f,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='records'},indent=2))


if __name__ == '__main__':
    main()
