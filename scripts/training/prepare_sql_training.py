"""Freeze a source-labelled corpus before training; outputs are private artifacts."""
import argparse
import hashlib
import json
from pathlib import Path
import pyarrow.parquet as pq
from sql_agent.training_dataset import prepare_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    dataset = prepare_dataset(pq.read_table(args.source).to_pylist(),
                              limits={'train':512,'dev':64,'test':100})
    if {k:len(v) for k,v in dataset['splits'].items()} != {'train':512,'dev':64,'test':100}:
        raise ValueError('insufficient valid samples; no dataset published')
    dataset['source_sha256'] = hashlib.sha256(args.source.read_bytes()).hexdigest()
    dataset['source'] = 'gretelai/synthetic_text_to_sql'
    dataset['source_revision'] = '740ab236e64503fba51be1101df7a1be83bf455d'
    dataset['license'] = 'Apache-2.0'
    with args.output.open('x') as stream:
        json.dump(dataset, stream, indent=2)
    print(json.dumps({k:len(v) for k,v in dataset['splits'].items()}))


if __name__ == '__main__':
    main()
