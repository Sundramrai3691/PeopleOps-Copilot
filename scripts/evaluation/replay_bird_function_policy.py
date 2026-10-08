"""Paired execution-policy replay of frozen BIRD proposals; no new model calls."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from sql_agent.bounded_runtime import ActionProposal,BoundedAgentRuntime
from sql_agent.data_agent import DataContract,SQLAgentSession
from scripts.evaluation.validate_bird_external import BenchmarkSession,file_hash,reference_rows,score


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
 baseline=Path('outputs/validation/bird_500_v2'); data=Path('data/local/bird_mini_dev'); databases=data/'databases_full'
 manifest=json.loads((baseline/'manifest.json').read_text());cases={r['question_id']:r for r in json.loads((data/'sqlite.jsonl').read_text())}
 assert file_hash(data/'sqlite.jsonl')==manifest['question_file_sha256']
 for item in manifest['source']['files']:assert file_hash(databases/item['path'])==item['sha256']
 args.output.mkdir(parents=True,exist_ok=False)
 source=Path('src/sql_agent/data_agent.py')
 protocol={'n':500,'design':'Replay final frozen proposals through modified read-only executor. No new generation or semantic screening; baseline reference errors stay unknown.',
  'function_allowlist':sorted(SQLAgentSession.FUNCTIONS),'source_sha256':file_hash(source),'runner_sha256':file_hash(Path(__file__)),
  'baseline_manifest_sha256':file_hash(baseline/'manifest.json'),'baseline_records_sha256':{str(i):file_hash(baseline/f'{i}.json') for i in manifest['question_ids']}}
 (args.output/'manifest.json').write_text(json.dumps(protocol,indent=2))
 rows=[]
 for i in manifest['question_ids']:
  b=json.loads((baseline/f'{i}.json').read_text());old=b['bounded'];proposals=[t['proposal'] for t in old['outcome']['trajectory'] if t['step']=='propose']
  r={'id':i,'before_correct':old['execution_match'],'before_decision':old['outcome']['decision'],'after_correct':False,'after_wrong':False,'unknown':bool(b.get('reference_error'))}
  if not proposals:r.update(after_decision='STOP',reason='no_frozen_proposal')
  else:
   path=databases/b['db_id']/(b['db_id']+'.sqlite');db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
   try:
    session=BenchmarkSession(db,DataContract(('dynamic_benchmark_output',),max_rows=10000))
    outcome=BoundedAgentRuntime().run(ActionProposal(**proposals[-1]),session)
    r.update(after_decision=outcome.decision,reason=outcome.reason,outcome=asdict(outcome),error=session.last_error)
    if outcome.decision=='KEEP' and not r['unknown']:
     if old['outcome']['decision']=='KEEP':
      assert json.loads(json.dumps(outcome.output))==old['outcome']['output'],'Previously accepted output drift'
      r.update(after_correct=old['execution_match'],after_wrong=old['false_accept'])
     else:
      try:
       expected=reference_rows(path,cases[i]['SQL']);graded=score(outcome,expected)
       r.update(after_correct=graded['execution_match'],after_wrong=graded['false_accept'])
      except Exception as exc:r.update(unknown=True,reference_error=str(exc))
   finally:db.close()
  rows.append(r);(args.output/f'{i}.json').write_text(json.dumps(r,indent=2))
 summary={'n':len(rows),'before_correct':sum(r['before_correct'] for r in rows),'after_correct':sum(r['after_correct'] for r in rows),
  'after_wrong_retained':sum(r['after_wrong'] for r in rows),'after_keep':sum(r['after_decision']=='KEEP' for r in rows),
  'newly_executable':[r['id'] for r in rows if r['before_decision']=='STOP' and r['after_decision']=='KEEP'],
  'new_correct':[r['id'] for r in rows if not r['before_correct'] and r['after_correct']],
  'lost_correct':[r['id'] for r in rows if r['before_correct'] and not r['after_correct']],
  'model_calls':0,'source_unchanged':file_hash(source)==protocol['source_sha256']}
 assert summary['source_unchanged']
 for item in manifest['source']['files']:assert file_hash(databases/item['path'])==item['sha256']
 summary['database_hashes_unchanged']=True
 (args.output/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary,indent=2))

if __name__=='__main__':main()
