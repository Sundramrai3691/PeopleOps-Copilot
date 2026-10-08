"""Replay every frozen billing candidate on 24 fresh data instances; no inference.

Known questions and schema, new data stress cases. Not a public benchmark score.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import random
import shutil
import sqlite3

from sql_agent.agent_evaluation import compare_output
from sql_agent.bounded_runtime import ActionProposal
from sql_agent.data_agent import SQLAgentSession
from scripts.evaluation import transfer_sql_cases as billing
from scripts.evaluation.render_model_comparison import ROOT, load_run


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(number):
    rng=random.Random(51000+number)
    invoices=[(1000+number*100+i,'account_'+str(i%4),rng.choice([0,50,100,100,200]),
               rng.choice([None,'2026-02-28','2026-03-01','2026-03-02'])) for i in range(8)]
    data={'invoices':invoices,'receipts':[],'credits':[]}
    def receipt(parent,amount,state):data['receipts'].append((len(data['receipts'])+1,parent,amount,state))
    def credit(parent,amount):data['credits'].append((len(data['credits'])+1,parent,amount))
    if number==0:return data  # Empty child tables.
    if number==1:
        data['invoices']=[(i,a,0,d) for i,a,_,d in invoices];return data  # All maxima tie; zero balances.
    if number==2:
        receipt(invoices[0][0],10,'settled');return data  # Exactly 12.5%, exposes integer division.
    if number==3:
        receipt(None,10,'settled');receipt(999999,30,'settled')
        receipt(invoices[0][0],None,'settled');credit(None,7)
        return data  # Nullable foreign key and NULL settled amount.
    if number==4:
        for _ in range(2):receipt(invoices[0][0],10,'settled');credit(invoices[0][0],3)
        receipt(invoices[0][0],999,'pending');receipt(invoices[1][0],None,'settled')
        return data  # Equal-valued records must count separately.
    for parent,_,_,_ in invoices:
        for _ in range(rng.randrange(5)):
            receipt(parent,rng.choice([None,0,5,10,10,25,100]),rng.choice(['settled','pending',None]))
        for _ in range(rng.randrange(4)):credit(parent,rng.choice([None,0,3,3,15,30]))
    return data


def score(case,sql,path,data):
    if not sql:return {'correct':False,'error':'no accepted original candidate','executed':False}
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as connection:
        try:
            session=SQLAgentSession(connection,case.task(path).contract)
            action=ActionProposal('REPAIR','sql_query',{'sql':sql})
            allowed,reason=session.authorize(action)
            if not allowed:return {'correct':False,'error':reason,'executed':False}
            output=session.execute(action)
            correct=compare_output(output,billing.expected(case,data))
            return {'correct':correct,'executed':True,**({} if correct else {'output':output})}
        except Exception as exc:
            return {'correct':False,'error':type(exc).__name__,'executed':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    names=('guided_coder14_billing_v2','quality_regression_v1','fast_sql_regression_v1')
    inputs={};episode_hashes={}
    for name in names:
        _,_,rows=load_run(name)
        inputs[name]=[r for r in rows if r['domain']=='billing']
        assert len(inputs[name])==48
        for p in (ROOT/'outputs/validation'/name).glob('episode_*.json'):episode_hashes[str(p.relative_to(ROOT))]=digest(p)
    sources=list((ROOT/'src/sql_agent').glob('*.py'))+[Path(__file__),Path(billing.__file__)]
    hashes={str(p.resolve().relative_to(ROOT)):digest(p) for p in sources}
    for p in sources:
        target=args.output/'source_snapshot'/p.resolve().relative_to(ROOT)
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,target)
    data={v:fixture(v) for v in range(24)};databases={}
    for v,values in data.items():
        path=(args.output/f'billing_{v:02d}.sqlite').resolve();databases[v]=path
        with sqlite3.connect(path) as connection:
            connection.executescript(billing.SCHEMA)
            for table,rows in values.items():
                if rows:connection.executemany(f'INSERT INTO {table} VALUES ({",".join("?" for _ in rows[0])})',rows)
        connection.close()
    dbhash={p.name:digest(p) for p in databases.values()}
    manifest={'scope':__doc__,'database_instances':24,'known_questions':8,'saved_candidates_per_configuration':48,
        'checks_per_configuration':48*24,'inference_calls':0,'seed_rule':'51000 + fixture number',
        'invoices_per_instance':8,'accounts_per_instance':4,'parent_amounts_non_null':True,
        'data_cases':{0:'empty child tables',1:'zero balances and tied maxima',2:'fractional percentage',
                      3:'nullable/orphan foreign keys and NULL settled amounts',4:'equal-valued distinct records',
                      '5-23':'seeded sparse/dense children, NULLs, date boundaries and ties'},
        'source_sha256':hashes,'source_episode_sha256':episode_hashes,'database_sha256':dbhash}
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    cases={c.case_id:c for c in billing.CASES};by_configuration={}
    for name,rows in inputs.items():
        details=[]
        for row in rows:
            assert row['pipeline']['result']['business_context']['question']==cases[row['case_id']].question
            assert not row['pipeline']['result']['business_context']['definitions'] and not row['auto_released']
            proposals=[t['proposal'] for t in row['pipeline']['result']['agent_trajectory']
                       if t['step']=='propose' and t['proposal'].get('tool')=='sql_query']
            sql=proposals[-1]['arguments']['sql'] if proposals and row['controller_accepted'] else None
            checks=[{'instance':v,**score(cases[row['case_id']],sql,databases[v],data[v])} for v in range(24)]
            details.append({'case_id':row['case_id'],'original_variant':row['variant'],'trial':row['trial'],
                'sql':sql,'original_correct':row['accepted_correct'],
                'original_both_instances_correct':row['same_query_correct_on_both_instances'],
                'all_new_instances_correct':all(c['correct'] for c in checks),
                'all_26_instances_correct':row['same_query_correct_on_both_instances'] and all(c['correct'] for c in checks),
                'checks':checks})
        (args.output/(name+'.json')).write_text(json.dumps(details,indent=2))
        by_configuration[name]={'saved_candidates':len(details),'original_correct':sum(d['original_correct'] for d in details),
            'original_both_instances_correct':sum(d['original_both_instances_correct'] for d in details),
            'all_24_instances_correct':sum(d['all_new_instances_correct'] for d in details),
            'all_26_instances_correct':sum(d['all_26_instances_correct'] for d in details),
            'lost_after_original_both_passed':sum(d['original_both_instances_correct'] and not d['all_new_instances_correct'] for d in details),
            'correct_checks':sum(c['correct'] for d in details for c in d['checks']),
            'total_checks':len(details)*24,'sql_execution_attempts':sum(c['executed'] for d in details for c in d['checks'])}
    summary={'by_configuration':by_configuration,'inference_calls':0,
        'sources_unchanged':all(digest(ROOT/p)==h for p,h in hashes.items()),
        'input_episodes_unchanged':all(digest(ROOT/p)==h for p,h in episode_hashes.items()),
        'databases_unchanged':all(digest(args.output/p)==h for p,h in dbhash.items())}
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2))
    assert summary['sources_unchanged'] and summary['input_episodes_unchanged'] and summary['databases_unchanged']
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
