"""Check an audited counts report; exit nonzero for missing, stale or failed evidence."""
import argparse, hashlib, json
from pathlib import Path
from sql_agent.evaluation_gate import assess


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--model-profile',type=Path,required=True,help='Canonical JSON with model digest and inference options')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[2]
    sources={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted((root/'src').rglob('*.py'))}
    fingerprint=hashlib.sha256(json.dumps(sources,sort_keys=True).encode()).hexdigest()
    try:data=json.loads(args.report.read_text())
    except (OSError,ValueError):data={}
    try:
        profile=json.loads(args.model_profile.read_text())
        if not isinstance(profile,dict) or not profile.get('model_digest') or not isinstance(profile.get('options'),dict):
            raise ValueError('invalid model profile')
        profile_id=hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()
    except (OSError,ValueError):profile_id=None
    result=assess(data,expected_configuration=fingerprint,expected_model_profile=profile_id)
    result['expected_configuration']=fingerprint
    result['expected_model_profile']=profile_id
    text=json.dumps(result,indent=2)
    if args.output:
        with args.output.open('x') as output:output.write(text+'\n')
    print(text)
    raise SystemExit(0 if result['passed'] else 1)


if __name__=='__main__':main()
