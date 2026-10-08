"""Application-owned metric definitions and bounded source-health checks."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import re

from .bounded_runtime import ActionProposal
from .execution_record import digest


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    terms: tuple[str, ...]
    definition: str
    source: str
    version: str
    required: bool = False

    def __post_init__(self):
        object.__setattr__(self, 'terms', tuple(self.terms))
        if not all((self.metric_id,self.definition,self.source,self.version)) or not self.terms:
            raise ValueError('metric definition requires ID, terms, definition, source and version')


@dataclass(frozen=True)
class QualityCheck:
    check_id: str
    sql: str
    kind: str  # violation_count or freshness
    maximum: float  # tolerated violations, or age in hours
    severity: str
    policy_reason: str

    def __post_init__(self):
        import math
        if self.kind not in {'violation_count','freshness'} or self.severity not in {'blocking','warning'}:
            raise ValueError('invalid quality check kind or severity')
        if not self.check_id or not self.sql or not self.policy_reason or not math.isfinite(self.maximum) or self.maximum < 0:
            raise ValueError('quality policy requires finite nonnegative budget and its rationale')


def context_hash(definitions, checks):
    return digest({'definitions':[asdict(d) for d in definitions], 'checks':[asdict(c) for c in checks]})


def retrieve_definitions(question, definitions):
    """Task-scoped literal term matching plus required definitions; no cross-task fallback."""
    result=[]
    for d in definitions:
        matched=[t for t in d.terms if re.search(r'(?<!\w)'+re.escape(t)+r'(?!\w)', question, re.I)]
        if matched or d.required:
            result.append({**asdict(d),'matched_terms':matched,'selection':'required' if d.required else 'term_match'})
    return result


def check_source_health(session, checks, *, now=None):
    now=now or datetime.now(timezone.utc)
    results=[]
    for check in checks:
        r={'check_id':check.check_id,'kind':check.kind,'severity':check.severity,
           'maximum':check.maximum,'policy_reason':check.policy_reason,'sql':check.sql}
        try:
            proposal=ActionProposal('REPAIR','sql_query',{'sql':check.sql},'registered_quality_check')
            allowed,reason=session.authorize(proposal)
            if not allowed:raise ValueError(reason)
            output=session.execute(proposal)
            if len(output['columns'])!=1 or len(output['rows'])!=1:
                raise ValueError('quality query must return exactly one scalar')
            value=output['rows'][0][0]
            if check.kind=='freshness':
                ts=datetime.fromisoformat(str(value).replace('Z','+00:00'))
                if ts.tzinfo is None:raise ValueError('freshness timestamp must include timezone')
                measurement=(now-ts.astimezone(timezone.utc)).total_seconds()/3600
            else:
                if not isinstance(value,(int,float)) or isinstance(value,bool):raise ValueError('expected numeric violation count')
                measurement=float(value)
                if not measurement.is_integer():raise ValueError('violation count must be integral')
            import math
            if not math.isfinite(measurement) or measurement<0:raise ValueError('invalid measurement')
            r.update(status='pass' if measurement<=check.maximum else 'fail',value=value,measurement=measurement,output_hash=digest(output))
        except Exception as exc:
            r.update(status='unknown',error_type=type(exc).__name__,error=str(exc)[:300])
        results.append(r)
    blocked=any(r['severity']=='blocking' and r['status']!='pass' for r in results)
    # Do not leak check SQL or errors into the model's repair history.
    session.last_sql=session.last_error=''
    return {'status':'blocked' if blocked else 'warning' if any(r['status']!='pass' for r in results) else 'pass' if checks else 'not_configured',
            'blocked':blocked,'checked_at':now.isoformat(),'checks':results,
            'scope':'Registered checks only; not a full data-quality certificate.'}
