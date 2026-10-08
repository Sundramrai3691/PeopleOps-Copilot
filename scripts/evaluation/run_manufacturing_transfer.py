"""New-schema paired check of the frozen SQL guidance change.

Manufacturing questions were not used to adjust the prompts. Operator families
remain familiar. Independent Python oracles are used only after inference.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import sqlite3
import urllib.request

from sql_agent.paired_benchmark import Case
from sql_agent.planner import OllamaPlannerModel
from scripts.evaluation import run_paired_sql_benchmark as runner

SCHEMA='''CREATE TABLE lots(id INTEGER PRIMARY KEY, plant TEXT, planned_units INTEGER);
CREATE TABLE runs(lot_id INTEGER, units INTEGER, result TEXT);
CREATE TABLE scrap(lot_id INTEGER, units INTEGER);'''
CASES=(
    Case('lot_balance','manufacturing','For every lot compute remaining_units = planned_units minus units from pass runs plus all scrap units. Count each run and scrap record once, even equal amounts. NULL and absent sums are zero. Return lot_id, remaining_units ordered by lot_id.',('lot_id','remaining_units')),
    Case('no_pass','manufacturing','List lot ids as lot_id with no run whose result is pass. Fail runs do not disqualify a lot. A pass run with NULL units does disqualify it. Order by lot_id.',('lot_id',)),
    Case('pass_lots','manufacturing','For every plant in lots count distinct lots having at least one pass run, even if units is NULL. Include zero counts. Return plant, pass_lots ordered by plant.',('plant','pass_lots')),
    Case('largest_lots','manufacturing','Within every plant list all lots tied for greatest planned_units. Return plant, lot_id, planned_units ordered by plant then lot_id.',('plant','lot_id','planned_units')),
    Case('plant_yield','manufacturing','For every plant calculate yield_percent = 100 times units from pass runs divided by units from all runs. Each run counts once, NULL units mean zero. Return zero for a zero denominator or no runs; round to two decimal places. Return plant, yield_percent ordered by plant.',('plant','yield_percent')),
    Case('positive_plants','manufacturing','For each plant net_units is total units from pass runs minus total scrap units. Each record counts once, including equal amounts; absent and NULL amounts mean zero. Return only plants whose net_units is strictly positive, as plant, net_units ordered by plant.',('plant','net_units')),
)


def fixture(v):
    return {'lots':[(1,'A',30),(2,'A',30),(3,'B',20),(4,'C',0),(5,'B',10)],
            'runs':[(1,10,'pass'),(1,10,'pass'),(1,99,'fail'),(2,8,'fail'),(3,12,'pass'),(5,None,'pass')]
                   + ([(2,5,'pass'),(3,4,'pass')] if v else []),
            'scrap':[(1,2),(1,3),(3,1)]+([(2,1)] if v else [])}


def expected(case,data):
    lots=data['lots'];runs=data['runs'];scrap=data['scrap'];plants=sorted({l[1] for l in lots})
    passed={r[0] for r in runs if r[2]=='pass'}
    good={l[0]:sum(r[1] or 0 for r in runs if r[0]==l[0] and r[2]=='pass') for l in lots}
    waste={l[0]:sum(r[1] or 0 for r in scrap if r[0]==l[0]) for l in lots}
    net={p:sum(good[l[0]]-waste[l[0]] for l in lots if l[1]==p) for p in plants}
    yields=[]
    for p in plants:
        total=sum(r[1] or 0 for l in lots if l[1]==p for r in runs if r[0]==l[0])
        numerator=sum(good[l[0]] for l in lots if l[1]==p)
        yields.append((p,round(100*numerator/total,2) if total else 0.0))
    answers={
        'lot_balance':[(l[0],l[2]-good[l[0]]+waste[l[0]]) for l in lots],
        'no_pass':[(l[0],) for l in lots if l[0] not in passed],
        'pass_lots':[(p,sum(l[0] in passed for l in lots if l[1]==p)) for p in plants],
        'largest_lots':sorted((l[1],l[0],l[2]) for l in lots if l[2]==max(x[2] for x in lots if x[1]==l[1])),
        'plant_yield':yields,'positive_plants':[(p,net[p]) for p in plants if net[p]>0]}
    return {'columns':case.columns,'rows':tuple(answers[case.case_id])}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    sources=list((runner.ROOT/'src/sql_agent').glob('*.py'))+[Path(__file__),Path(runner.__file__)]
    hashes={str(p.resolve().relative_to(runner.ROOT)):runner.sha_bytes(p.read_bytes()) for p in sources}
    for source in sources:
        target=args.output/'source_snapshot'/source.resolve().relative_to(runner.ROOT)
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    data={v:fixture(v) for v in range(2)};dbs={}
    for v in range(2):
        dbs[v]=(args.output/f'manufacturing_{v}.sqlite').resolve()
        with sqlite3.connect(dbs[v]) as db:
            db.executescript(SCHEMA)
            for table,rows in data[v].items():
                db.executemany(f'INSERT INTO {table} VALUES (?,?,?)' if table!='scrap' else 'INSERT INTO scrap VALUES (?,?)',rows)
    model_name='qwen2.5-coder:14b'
    with urllib.request.urlopen('http://127.0.0.1:11434/api/tags',timeout=10) as r:
        model=next(m for m in json.load(r)['models'] if m['name']==model_name)
    methods=('native_sql','native_sql_guided')
    schedule=[]
    for t in range(3):
        for c in CASES:
            for v in range(2):
                for method in methods[::(-1 if (t+v)%2 else 1)]:schedule.append((c,v,t,method))
    dbhash={p.name:runner.sha_bytes(p.read_bytes()) for p in dbs.values()}
    runner.write_json(args.output/'manifest.json',{'source_sha256':hashes,'database_sha256':dbhash,'model':model,
        'cases':[asdict(c) for c in CASES],'episodes_planned':len(schedule),
        'schedule':[(c.case_id,v,t,m) for c,v,t,m in schedule],
        'protocol':'Both methods: same coder14 Q4_K_M, native SQL protocol, temperature=0, max_tokens=2048, one SQL round; alternating method order.',
        'scope':'New schema/questions after guidance was frozen; known SQL operator motifs, not a public benchmark or guarantee of model-training novelty.'})
    runner.expected=expected;rows=[]
    delegate=OllamaPlannerModel('http://127.0.0.1:11434',model_name,max_tokens=2048,json_mode=False)
    for index,(case,v,t,method) in enumerate(schedule):
        row=runner.run_episode(case,v,method,'clean',t,None,dbs,data,delegate)
        rows.append(row);runner.write_json(args.output/f'episode_{index:04d}.json',row)
        print(json.dumps({'done':index+1,'planned':len(schedule),'case':case.case_id,'method':method,'correct':row['accepted_correct']}),flush=True)
    with urllib.request.urlopen('http://127.0.0.1:11434/api/tags',timeout=10) as r:
        digest=next(m['digest'] for m in json.load(r)['models'] if m['name']==model_name)
    summary={'episodes':len(rows),'by_method':{m:runner.summarize([r for r in rows if r['method']==m],3) for m in methods},
             'paired_difference':runner.paired_difference(rows,'native_sql_guided','native_sql'),
             'sources_unchanged':all(runner.sha_bytes((runner.ROOT/p).read_bytes())==h for p,h in hashes.items()),
             'databases_unchanged':all(runner.sha_bytes((args.output/p).read_bytes())==h for p,h in dbhash.items()),
             'model_unchanged':digest==model['digest']}
    runner.write_json(args.output/'summary.json',summary)
    assert summary['sources_unchanged'] and summary['databases_unchanged'] and summary['model_unchanged']
    print(json.dumps(summary),flush=True)


if __name__=='__main__':main()
