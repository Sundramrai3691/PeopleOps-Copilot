"""Author new synthetic evaluation only after runtime freeze. Gold stays external."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random
import sqlite3


class Suite:
    def __init__(self, output):
        self.out=output;output.mkdir(parents=True,exist_ok=False)
        (output/'business').mkdir();self.cases=[];self.docs=[];self.policies={};self.checks=[]
    def database(self, ident, ddl, tables):
        path=self.out/'business'/(ident+'.db')
        with sqlite3.connect(path) as db:
            db.executescript(ddl)
            for name,rows in tables.items():
                if rows:db.executemany('INSERT INTO '+name+' VALUES('+','.join('?' for _ in rows[0])+')',rows)
        self.policies[ident]={'database':'/business/'+ident+'.db','tables':list(tables),'max_rows':200,'allow_ddl':False}
        return path
    def case(self, db, stratum, domain, family, question, columns, rows, reference, rule, tables):
        # Independently compute expected rows in Python, then check a separately written SQL oracle.
        with sqlite3.connect(self.out/'business'/(db+'.db')) as conn:
            cur=conn.execute(reference);sql_rows=[list(r) for r in cur.fetchall()]
            sql_columns=[c[0] for c in cur.description]
        rows=[list(r) for r in rows]
        if sql_rows!=rows or sql_columns!=columns:raise ValueError((db,family,sql_columns,sql_rows,columns,rows))
        ident=db+':'+family
        self.cases.append({'case_id':ident,'database_id':db,'stratum':stratum,'domain':domain,
            'family':domain+':'+family,'question':question,'expected':{'columns':columns,'rows':rows},
            'relevant_ids':[family],'required_tables':tables})
        self.docs.append({'id':family,'database_id':db,'title':family.replace('_',' '),'text':rule,
            'tables':tables,'version':'frozen-holdout-v1','source':'synthetic/operator-authored-handbook'})
        self.checks.append({'case_id':ident,'python_sql_oracle_agreement':True,'reference_sql':reference})
    def save(self):
        random.Random(735019).shuffle(self.cases)
        manifest={'scope':'Post-runtime-freeze synthetic model-blind evaluation; operator-authored, not independently sealed. New structural schemas and rules, with repeated data variants clustered.',
            'cases':self.cases,'database_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.out/'business').glob('*.db')}}
        for name,data in [('evaluation.json',manifest),('oracle_checks.json',self.checks),('knowledge.json',{'documents':self.docs}),('mutations.json',{'control_store':'/control/changes.db','databases':self.policies})]:
            (self.out/name).write_text(json.dumps(data,indent=2))
        (self.out/'sealed_inputs.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.out.glob('*.json')},indent=2))


def inventory(s,variant):
    rng=random.Random(813011+variant);db='inventory_'+str(variant);k=rng.randint(2,8)
    depots=[(1,'North'),(2,'South'),(3,'Empty')];products=[(1,'A'),(2,'B'),(3,'C')]
    moves=[(1,1,1,k,'posted'),(2,1,1,-k,'posted'),(3,1,2,2*k,'posted'),(4,2,1,k,'posted'),(5,2,1,-1,'posted'),(6,2,2,9*k,'pending'),(7,1,3,-2,'posted'),(8,1,3,2,'pending')]
    s.database(db,'CREATE TABLE depots(depot_id INTEGER PRIMARY KEY,label TEXT); CREATE TABLE products(product_id INTEGER PRIMARY KEY,sku TEXT); CREATE TABLE movements(move_id INTEGER PRIMARY KEY,depot_id INTEGER,product_id INTEGER,delta_units INTEGER,state TEXT);',{'depots':depots,'products':products,'movements':moves})
    totals=defaultdict(int)
    for _,d,p,v,status in moves:
        if status=='posted':totals[d,p]+=v
    rule='Inventory uses signed delta_units of posted movements only. Pending movements do not affect stock. A depot-product pair with no movements has zero units.'
    s.case(db,'blind_holdout','inventory','stock_matrix','For every depot and every product, return depot_id, product_id, net_units under the inventory convention, including zero and negative stock. Sort by depot_id, product_id.', ['depot_id','product_id','net_units'],[(d[0],p[0],totals[d[0],p[0]]) for d in depots for p in products],"SELECT d.depot_id,p.product_id,COALESCE(SUM(m.delta_units),0) AS net_units FROM depots d CROSS JOIN products p LEFT JOIN movements m ON m.depot_id=d.depot_id AND m.product_id=p.product_id AND m.state='posted' GROUP BY d.depot_id,p.product_id ORDER BY d.depot_id,p.product_id",rule,['depots','products','movements'])
    s.case(db,'blind_holdout','inventory','nonpositive_pairs','List every depot-product pair with net inventory at most zero, including pairs with no movement. Return depot_id, product_id, sorted by both.', ['depot_id','product_id'],[(d[0],p[0]) for d in depots for p in products if totals[d[0],p[0]]<=0],"SELECT d.depot_id,p.product_id FROM depots d CROSS JOIN products p LEFT JOIN movements m ON m.depot_id=d.depot_id AND m.product_id=p.product_id AND m.state='posted' GROUP BY d.depot_id,p.product_id HAVING COALESCE(SUM(m.delta_units),0)<=0 ORDER BY d.depot_id,p.product_id",rule,['depots','products','movements'])
    s.case(db,'blind_holdout','inventory','positive_depots','For each product, count depots with strictly positive net inventory. Include products with zero such depots. Return product_id, depot_count, sorted by product_id.', ['product_id','depot_count'],[(p[0],sum(totals[d[0],p[0]]>0 for d in depots)) for p in products],"WITH stock AS (SELECT product_id,depot_id,SUM(delta_units) AS units FROM movements WHERE state='posted' GROUP BY product_id,depot_id) SELECT p.product_id,COUNT(s.depot_id) AS depot_count FROM products p LEFT JOIN stock s ON p.product_id=s.product_id AND s.units>0 GROUP BY p.product_id ORDER BY p.product_id",rule,['products','movements'])
    s.case(db,'blind_holdout','inventory','pending_exposure','For every depot, sum the absolute size of individual pending movements, using zero if none. Return depot_id, pending_units, sorted by depot_id.', ['depot_id','pending_units'],[(d[0],sum(abs(m[3]) for m in moves if m[1]==d[0] and m[4]=='pending')) for d in depots],"SELECT d.depot_id,COALESCE(SUM(ABS(m.delta_units)),0) AS pending_units FROM depots d LEFT JOIN movements m ON d.depot_id=m.depot_id AND m.state='pending' GROUP BY d.depot_id ORDER BY d.depot_id",'Pending exposure sums ABS(delta_units) per pending movement, before aggregation; include every depot.', ['depots','movements'])
    s.case(db,'blind_holdout','inventory','outbound_units','For every product, report outbound_units as the magnitude of negative posted movements only. Include zero when absent. Return product_id, outbound_units, sorted by product_id.', ['product_id','outbound_units'],[(p[0],sum(-m[3] for m in moves if m[2]==p[0] and m[4]=='posted' and m[3]<0)) for p in products],"SELECT p.product_id,COALESCE(SUM(-m.delta_units),0) AS outbound_units FROM products p LEFT JOIN movements m ON p.product_id=m.product_id AND m.state='posted' AND m.delta_units<0 GROUP BY p.product_id ORDER BY p.product_id",'Outbound units sum magnitudes of individual negative posted movements. Positive movements do not cancel outbound activity.', ['products','movements'])


def clinic(s,v):
    db='clinic_'+str(v);k=v+1
    patients=[(1,'Ada'),(2,'Bo'),(3,'Cy'),(4,'Di')];clinicians=[(1,'A'),(2,'B'),(3,'C')]
    visits=[(1,1,1,'2026-06-01','done',10*k),(2,1,1,'2026-06-03','cancelled',90),(3,2,1,'2026-06-02','done',30*k),(4,2,2,'2026-05-30','done',20*k),(5,3,2,'2026-06-05','booked',None),(6,1,2,'2026-06-07','done',None)]
    s.database(db,'CREATE TABLE patients(patient_id INTEGER PRIMARY KEY,name TEXT);CREATE TABLE clinicians(clinician_id INTEGER PRIMARY KEY,name TEXT);CREATE TABLE encounters(encounter_id INTEGER PRIMARY KEY,patient_id INTEGER,clinician_id INTEGER,started_at TEXT,state TEXT,duration_min INTEGER);',{'patients':patients,'clinicians':clinicians,'encounters':visits})
    rule='Done encounters count as completed. Booked and cancelled encounters are not completed. Duration averages ignore NULL; do not replace missing durations with zero.'
    s.case(db,'blind_holdout','clinic','completion_fraction','For every clinician return clinician_id, completed_count, scheduled_count. Scheduled count includes all encounters regardless of state; completed count includes done only. Include clinicians with no encounters; sort by clinician_id.', ['clinician_id','completed_count','scheduled_count'],[(c[0],sum(e[2]==c[0] and e[4]=='done' for e in visits),sum(e[2]==c[0] for e in visits)) for c in clinicians],"SELECT c.clinician_id,SUM(CASE WHEN e.state='done' THEN 1 ELSE 0 END) AS completed_count,COUNT(e.encounter_id) AS scheduled_count FROM clinicians c LEFT JOIN encounters e ON c.clinician_id=e.clinician_id GROUP BY c.clinician_id ORDER BY c.clinician_id",rule,['clinicians','encounters'])
    s.case(db,'blind_holdout','clinic','first_completed','For every patient return patient_id and first_completed_at, the earliest started_at of a done encounter, or NULL if none. Sort by patient_id.', ['patient_id','first_completed_at'],[(p[0],min([e[3] for e in visits if e[1]==p[0] and e[4]=='done'],default=None)) for p in patients],"SELECT p.patient_id,MIN(e.started_at) AS first_completed_at FROM patients p LEFT JOIN encounters e ON p.patient_id=e.patient_id AND e.state='done' GROUP BY p.patient_id ORDER BY p.patient_id",rule,['patients','encounters'])
    averages=[]
    for c in clinicians:
        values=[e[5] for e in visits if e[2]==c[0] and e[4]=='done' and e[5] is not None]
        averages.append((c[0],sum(values)/len(values) if values else None))
    s.case(db,'blind_holdout','clinic','mean_duration','For every clinician return clinician_id, avg_minutes for done encounters. Ignore NULL duration_min; return NULL when no duration is known. Sort by clinician_id.', ['clinician_id','avg_minutes'],averages,"SELECT c.clinician_id,AVG(e.duration_min) AS avg_minutes FROM clinicians c LEFT JOIN encounters e ON c.clinician_id=e.clinician_id AND e.state='done' GROUP BY c.clinician_id ORDER BY c.clinician_id",rule,['clinicians','encounters'])
    s.case(db,'blind_holdout','clinic','shared_care','List patients who completed encounters with at least two different clinicians. Return patient_id, clinician_count, sorted by patient_id.', ['patient_id','clinician_count'],[(p[0],len({e[2] for e in visits if e[1]==p[0] and e[4]=='done'})) for p in patients if len({e[2] for e in visits if e[1]==p[0] and e[4]=='done'})>=2],"SELECT patient_id,COUNT(DISTINCT clinician_id) AS clinician_count FROM encounters WHERE state='done' GROUP BY patient_id HAVING COUNT(DISTINCT clinician_id)>=2 ORDER BY patient_id",rule,['encounters'])
    s.case(db,'blind_holdout','clinic','month_window','List done encounters starting on or after 2026-06-01 and before 2026-07-01. Return encounter_id, patient_id, sorted by encounter_id.', ['encounter_id','patient_id'],[(e[0],e[1]) for e in visits if e[4]=='done' and '2026-06-01'<=e[3]<'2026-07-01'],"SELECT encounter_id,patient_id FROM encounters WHERE state='done' AND started_at>='2026-06-01' AND started_at<'2026-07-01' ORDER BY encounter_id",rule,['encounters'])


def subscriptions(s,v):
    db='subscription_'+str(v);k=100*(v+1)
    accounts=[(1,'A'),(2,'B'),(3,'C'),(4,'D')];plans=[(1,'base'),(2,'extra')]
    subs=[(1,1,1,'2026-01-01',None),(2,1,2,'2026-03-01','2026-06-01'),(3,2,1,'2026-06-01',None),(4,3,2,'2026-07-01',None),(5,2,2,'2026-05-01',None)]
    charges=[(1,1,k,'settled','2026-06-01'),(2,1,k,'settled','2026-06-10'),(3,2,9*k,'settled','2026-05-10'),(4,3,2*k,'pending','2026-06-02'),(5,5,3*k,'settled','2026-06-03')]
    s.database(db,'CREATE TABLE accounts(account_id INTEGER PRIMARY KEY,name TEXT);CREATE TABLE plans(plan_id INTEGER PRIMARY KEY,label TEXT);CREATE TABLE subscriptions(subscription_id INTEGER PRIMARY KEY,account_id INTEGER,plan_id INTEGER,starts_at TEXT,ends_at TEXT);CREATE TABLE charges(charge_id INTEGER PRIMARY KEY,subscription_id INTEGER,amount_cents INTEGER,state TEXT,charged_at TEXT);',{'accounts':accounts,'plans':plans,'subscriptions':subs,'charges':charges})
    active=[r for r in subs if r[3]<='2026-06-01' and (r[4] is None or r[4]>'2026-06-01')];activeids={r[0] for r in active}
    rule='At an as-of date a subscription is active when starts_at is inclusive and ends_at is exclusive; NULL ends_at means unbounded. Only settled charges count as collected. Multiple active subscriptions per account are allowed.'
    s.case(db,'blind_holdout','subscription','active_plans','As of 2026-06-01, for every account return account_id, active_plan_count counting distinct active plan_id values. Include zero and sort by account_id.', ['account_id','active_plan_count'],[(a[0],len({r[2] for r in active if r[1]==a[0]})) for a in accounts],"SELECT a.account_id,COUNT(DISTINCT s.plan_id) AS active_plan_count FROM accounts a LEFT JOIN subscriptions s ON a.account_id=s.account_id AND s.starts_at<='2026-06-01' AND (s.ends_at IS NULL OR s.ends_at>'2026-06-01') GROUP BY a.account_id ORDER BY a.account_id",rule,['accounts','subscriptions'])
    s.case(db,'blind_holdout','subscription','active_collected','As of 2026-06-01, for every account sum all settled charge amounts linked to subscriptions active on that date, regardless of charge date. Return account_id, collected_cents, zero if absent, sorted by account_id.', ['account_id','collected_cents'],[(a[0],sum(c[2] for r in active if r[1]==a[0] for c in charges if c[1]==r[0] and c[3]=='settled')) for a in accounts],"SELECT a.account_id,COALESCE(SUM(c.amount_cents),0) AS collected_cents FROM accounts a LEFT JOIN subscriptions s ON a.account_id=s.account_id AND s.starts_at<='2026-06-01' AND (s.ends_at IS NULL OR s.ends_at>'2026-06-01') LEFT JOIN charges c ON c.subscription_id=s.subscription_id AND c.state='settled' GROUP BY a.account_id ORDER BY a.account_id",rule,['accounts','subscriptions','charges'])
    s.case(db,'blind_holdout','subscription','uncollected_active','List subscriptions active on 2026-06-01 that have no settled charge, even if a pending charge exists. Return subscription_id, account_id, sorted by subscription_id.', ['subscription_id','account_id'],[(r[0],r[1]) for r in active if not any(c[1]==r[0] and c[3]=='settled' for c in charges)],"SELECT s.subscription_id,s.account_id FROM subscriptions s WHERE s.starts_at<='2026-06-01' AND (s.ends_at IS NULL OR s.ends_at>'2026-06-01') AND NOT EXISTS(SELECT 1 FROM charges c WHERE c.subscription_id=s.subscription_id AND c.state='settled') ORDER BY s.subscription_id",rule,['subscriptions','charges'])
    s.case(db,'blind_holdout','subscription','plan_population','For every plan report plan_id, account_count counting distinct accounts with that plan active on 2026-06-01. Include zero and sort by plan_id.', ['plan_id','account_count'],[(p[0],len({r[1] for r in active if r[2]==p[0]})) for p in plans],"SELECT p.plan_id,COUNT(DISTINCT s.account_id) AS account_count FROM plans p LEFT JOIN subscriptions s ON p.plan_id=s.plan_id AND s.starts_at<='2026-06-01' AND (s.ends_at IS NULL OR s.ends_at>'2026-06-01') GROUP BY p.plan_id ORDER BY p.plan_id",rule,['plans','subscriptions'])
    sums=defaultdict(int)
    for c in charges:
        if c[3]=='settled':sums[c[4][:7]]+=c[2]
    s.case(db,'blind_holdout','subscription','collections_month','Across all subscriptions, sum settled charges by month YYYY-MM of charged_at. Return month, collected_cents, sorted by month; omit months without settled charges.', ['month','collected_cents'],sorted(sums.items()),"SELECT SUBSTR(charged_at,1,7) AS month,SUM(amount_cents) AS collected_cents FROM charges WHERE state='settled' GROUP BY SUBSTR(charged_at,1,7) ORDER BY month",rule,['charges'])


def ranking(s,v,scenario):
    db=f'ranking_{scenario}_{v}';k=10+v
    players=[(1,'east'),(2,'east'),(3,'east'),(4,'west'),(5,'west'),(6,'west')]
    totals={1:3*k,2:3*k,3:2*k,4:k,5:k,6:0}
    if scenario=='all_zero':totals={i:0 for i,_ in players}
    if scenario=='negative_with_absent':totals={i:-i*k for i,_ in players};totals[6]=0
    scores=[]
    for i,_ in players:
        if i!=6:
            scores.extend([(len(scores)+1,i,totals[i]//2,'valid'),(len(scores)+2,i,totals[i]-totals[i]//2,'valid')])
    scores.append((len(scores)+1,6,999,'void'))
    s.database(db,'CREATE TABLE players(player_id INTEGER PRIMARY KEY,region TEXT);CREATE TABLE scores(score_id INTEGER PRIMARY KEY,player_id INTEGER,points INTEGER,state TEXT);',{'players':players,'scores':scores})
    rule='Player totals sum valid score points; void scores do not count. Include every player; absent valid scores total zero. Ties are equal numeric totals, independent of player_id.'
    cte="WITH totals AS (SELECT p.player_id,p.region,COALESCE(SUM(s.points),0) AS points FROM players p LEFT JOIN scores s ON p.player_id=s.player_id AND s.state='valid' GROUP BY p.player_id,p.region) "
    if scenario in ('global_max','all_zero','negative_with_absent'):
        q='Return all players tied for the largest total valid points, including players without valid scores as zero. Return player_id, points, sorted by player_id.'
        rows=[(i,x) for i,x in totals.items() if x==max(totals.values())]
        ref=cte+'SELECT player_id,points FROM totals WHERE points=(SELECT MAX(points) FROM totals) ORDER BY player_id';cols=['player_id','points']
    elif scenario=='dense_top_two':
        q='Return players in the top two distinct total-point levels (dense ranking), including all ties. All players participate with absent valid scores as zero. Return player_id, points, sorted by player_id.'
        levels=sorted(set(totals.values()),reverse=True)[:2];rows=[(i,x) for i,x in totals.items() if x in levels]
        ref=cte+", ranked AS (SELECT *,DENSE_RANK() OVER(ORDER BY points DESC) AS rank_value FROM totals) SELECT player_id,points FROM ranked WHERE rank_value<=2 ORDER BY player_id";cols=['player_id','points']
    elif scenario=='competition_top_two':
        q='Return players with competition rank at most 2 by total valid points. Competition ranking is 1,1,3 after a two-way first-place tie, not dense ranking. Include all ties and all players with absent scores zero. Return player_id, points, sorted by player_id.'
        rows=[(i,x) for i,x in totals.items() if 1+sum(y>x for y in totals.values())<=2]
        ref=cte+", ranked AS (SELECT *,RANK() OVER(ORDER BY points DESC) AS rank_value FROM totals) SELECT player_id,points FROM ranked WHERE rank_value<=2 ORDER BY player_id";cols=['player_id','points']
    else:
        q='Within each region return every player tied for the largest total valid points in that region. Include players without valid scores as zero. Return region, player_id, points, sorted by region then player_id.'
        rows=sorted((region,i,totals[i]) for i,region in players if totals[i]==max(totals[j] for j,r in players if r==region))
        ref=cte+", ranked AS (SELECT *,RANK() OVER(PARTITION BY region ORDER BY points DESC) AS rank_value FROM totals) SELECT region,player_id,points FROM ranked WHERE rank_value=1 ORDER BY region,player_id";cols=['region','player_id','points']
    s.case(db,'tie_ranking','ranking',scenario,q,cols,rows,ref,rule,['players','scores'])


def correlated(s,v):
    db='risk_'+str(v);k=10+v
    members=[(1,'A'),(2,'B'),(3,'C'),(4,'D')]
    events=[(1,1,k,'eligible','2026-06-01'),(2,1,k,'eligible','2026-06-15'),(3,1,9*k,'excluded','2026-07-01'),(4,2,3*k,'eligible','2026-06-30'),(5,2,7*k,'excluded','2026-05-31'),(6,3,k,'excluded','2026-06-20'),(7,None,99*k,'eligible','2026-06-10')]
    bonuses=[(1,1,2,'approved'),(2,1,3,'approved'),(3,2,8,'pending')]
    s.database(db,'CREATE TABLE members(member_id INTEGER PRIMARY KEY,name TEXT);CREATE TABLE events(event_id INTEGER PRIMARY KEY,member_id INTEGER,units INTEGER,state TEXT,occurred_at TEXT);CREATE TABLE bonuses(bonus_id INTEGER PRIMARY KEY,member_id INTEGER,units INTEGER,state TEXT);',{'members':members,'events':events,'bonuses':bonuses})
    rule='Eligible events count; excluded events do not. Events with NULL member_id belong to nobody and never qualify any member. Approved bonuses are separate additive facts. Equal amounts on distinct rows count separately.'
    s.case(db,'correlated_risk','membership','fanout_equal_amounts','For every member return member_id, total_units: sum eligible event units plus approved bonus units, counting each fact once even when amounts are equal. Use zero for absent facts; sort by member_id.', ['member_id','total_units'],[(i,sum(e[2] for e in events if e[1]==i and e[3]=='eligible')+sum(b[2] for b in bonuses if b[1]==i and b[3]=='approved')) for i,_ in members],"SELECT m.member_id,COALESCE((SELECT SUM(e.units) FROM events e WHERE e.member_id=m.member_id AND e.state='eligible'),0)+COALESCE((SELECT SUM(b.units) FROM bonuses b WHERE b.member_id=m.member_id AND b.state='approved'),0) AS total_units FROM members m ORDER BY m.member_id",rule,['members','events','bonuses'])
    noeligible=[(i,) for i,_ in members if not any(e[1]==i and e[3]=='eligible' for e in events)]
    s.case(db,'correlated_risk','membership','null_antijoin','List members with no eligible event. The events table may contain NULL member_id, and excluded events do not count. Return member_id, sorted by member_id.', ['member_id'],noeligible,"SELECT m.member_id FROM members m WHERE NOT EXISTS(SELECT 1 FROM events e WHERE e.member_id=m.member_id AND e.state='eligible') ORDER BY m.member_id",rule,['members','events'])
    s.case(db,'correlated_risk','membership','mixed_eligibility','List members who have at least one excluded event and no eligible event. Return member_id, sorted by member_id.', ['member_id'],[(i,) for i,_ in members if any(e[1]==i and e[3]=='excluded' for e in events) and not any(e[1]==i and e[3]=='eligible' for e in events)],"SELECT m.member_id FROM members m WHERE EXISTS(SELECT 1 FROM events e WHERE e.member_id=m.member_id AND e.state='excluded') AND NOT EXISTS(SELECT 1 FROM events e WHERE e.member_id=m.member_id AND e.state='eligible') ORDER BY m.member_id",rule,['members','events'])
    s.case(db,'correlated_risk','membership','count_empty','For every member return member_id, event_count counting eligible events only; zero for no eligible events. Sort by member_id.', ['member_id','event_count'],[(i,sum(e[1]==i and e[3]=='eligible' for e in events)) for i,_ in members],"SELECT m.member_id,COUNT(e.event_id) AS event_count FROM members m LEFT JOIN events e ON e.member_id=m.member_id AND e.state='eligible' GROUP BY m.member_id ORDER BY m.member_id",rule,['members','events'])
    values=[e[2] for e in events if e[1] is not None and e[3]=='eligible']
    s.case(db,'correlated_risk','membership','weighted_mean','Return one column avg_units: average units over eligible events belonging to a registered member, weighting each event equally, not each member equally. Ignore unassigned events.', ['avg_units'],[(sum(values)/len(values),)],"SELECT AVG(e.units) AS avg_units FROM events e JOIN members m ON e.member_id=m.member_id WHERE e.state='eligible'",rule,['members','events'])
    s.case(db,'correlated_risk','membership','half_open_window','For every member sum eligible event units on or after 2026-06-01 and before 2026-07-01. Return member_id, june_units, zero if none, sorted by member_id.', ['member_id','june_units'],[(i,sum(e[2] for e in events if e[1]==i and e[3]=='eligible' and '2026-06-01'<=e[4]<'2026-07-01')) for i,_ in members],"SELECT m.member_id,COALESCE(SUM(e.units),0) AS june_units FROM members m LEFT JOIN events e ON e.member_id=m.member_id AND e.state='eligible' AND e.occurred_at>='2026-06-01' AND e.occurred_at<'2026-07-01' GROUP BY m.member_id ORDER BY m.member_id",rule,['members','events'])


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--freeze',type=Path,required=True);args=p.parse_args()
    frozen=json.loads(args.freeze.read_text())
    if any(hashlib.sha256(Path(p).read_bytes()).hexdigest()!=sha for p,sha in frozen['source_sha256'].items()):raise ValueError('Runtime changed after freeze')
    s=Suite(args.output)
    for v in range(4):
        inventory(s,v);clinic(s,v);subscriptions(s,v);correlated(s,v)
        for scenario in ('global_max','dense_top_two','competition_top_two','per_region','all_zero','negative_with_absent'):ranking(s,v,scenario)
    if len(s.cases)!=108:raise ValueError('Incomplete planned suite')
    s.save();print(json.dumps({'cases':len(s.cases),'oracle_crosschecks':len(s.checks),'runtime_unchanged':True}))


if __name__=='__main__':main()
