"""Explicit denominators for one frozen evaluation; never a release gate."""
from collections import defaultdict
import math
import random
from sql_agent.acceptance_report import summarize


def fraction(n, d):
    return {'numerator':n,'denominator':d,'rate':n/d if d else None}


def metrics(rows, *, seed=42, resamples=10000):
    summary=summarize(rows)
    candidates=[r for r in rows if r.get('has_candidate')]
    wrong=[r for r in candidates if not r['correct']]
    agreed=[r for r in candidates if r['verifier_status']=='agreement']
    false_accept=sum(not r['correct'] for r in agreed)
    released=[r for r in rows if r['status']=='completed']
    groups=defaultdict(list)
    for row in rows:groups[row['family']].append(int(row['correct']))
    clusters=list(groups.values());rng=random.Random(seed);draws=[]
    for _ in range(resamples):
        chosen=[rng.choice(clusters) for _ in clusters]
        draws.append(sum(map(sum,chosen))/sum(map(len,chosen)))
    draws.sort()
    summary.update(task_accuracy=fraction(sum(r['correct'] for r in rows),len(rows)),
        verifier_false_accept_rate=fraction(false_accept,len(wrong)),
        verifier_false_discovery_rate=fraction(false_accept,len(agreed)),
        observed_joint_wrong_agreement_rate=fraction(false_accept,len(rows)),
        system_false_accept_rate=fraction(sum(not r['correct'] for r in released),len(wrong)),
        system_false_discovery_rate=fraction(sum(not r['correct'] for r in released),len(released)),
        no_candidate=fraction(len(rows)-len(candidates),len(rows)),
        review=fraction(sum(r['status']=='needs_review' for r in rows),len(rows)),
        accuracy_cluster95=[draws[math.floor(.025*resamples)],draws[math.ceil(.975*resamples)-1]],
        question_template_clusters=len(clusters),
        latency_scope='Client submission to terminal observation, including cold start, polling and failures; timeout records are censored.',
        timed_out_records=sum(r.get('error_type')=='TimeoutError' for r in rows),
        p99_seconds=sorted(r['seconds'] for r in rows)[math.ceil(.99*len(rows))-1])
    return summary
