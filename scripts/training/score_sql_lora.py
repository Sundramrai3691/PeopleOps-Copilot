"""Re-score immutable raw responses using the runtime's JSON-fence handling."""
import argparse
import hashlib
import json
from pathlib import Path
from sql_agent.training_dataset import score_response
from scripts.training.train_sql_lora import wilson


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.dataset.read_text())['splits']['test']
    source = {r['id']:r for r in data}
    manifest = json.loads((args.run/'manifest.json').read_text())
    if manifest['dataset_sha256'] != hashlib.sha256(args.dataset.read_bytes()).hexdigest():
        raise ValueError('dataset changed')
    scores = {}
    raw_hashes = {}
    for name in ('baseline', 'adapter'):
        path = args.run/(name+'.json')
        raw_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        responses = json.loads(path.read_text())
        if [r['id'] for r in responses] != [r['id'] for r in data]:
            raise ValueError('incomplete or reordered evaluation')
        scores[name] = [{'id':r['id'], **score_response(source[r['id']], r['response'])} for r in responses]
    n = len(data)
    summary = {name:{'n':n,'correct':sum(r['correct'] for r in rows),
                    'strict_json':sum(r['strict_json'] for r in rows),
                    'wilson95':wilson(sum(r['correct'] for r in rows),n)}
               for name,rows in scores.items()}
    summary.update(fixed=sum(not b['correct'] and a['correct'] for b,a in zip(scores['baseline'],scores['adapter'])),
                   regressed=sum(b['correct'] and not a['correct'] for b,a in zip(scores['baseline'],scores['adapter'])))
    output = {'protocol_note':'Post-hoc parser alignment identified from baseline formatting; same fence removal applied to both saved response sets. No regeneration or model retuning.',
              'raw_sha256':raw_hashes, 'summary':summary,'records':scores}
    with (args.run/'runtime_aligned_scores.json').open('x') as stream:
        json.dump(output,stream,indent=2)
    print(json.dumps(summary,indent=2))


if __name__ == '__main__':
    main()
