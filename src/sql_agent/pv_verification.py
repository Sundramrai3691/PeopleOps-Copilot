"""Conservative PV-SQL-inspired verification, not a full PV-SQL reproduction.

Independent implementation of explicit-evidence checks. No LLM critique, gold SQL,
keyword-based DISTINCT/LIMIT inference, or free-form query regeneration.

Reference inspected: https://github.com/magic-YuanTian/PV-SQL
commit 9f2bcd9a16ec1029649222851bd9e08a2af3e2c6, pvsql/pv_sql.py.
This first-stage adapter handles explicit benchmark evidence only; it does not
implement the paper's model-driven probing or general natural-language constraints.
"""
import re
from sqlglot import exp, parse

# Only explicit, qualified string equalities. Unqualified prose is not a contract.
_BINDING = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*=\s*'((?:[^']|'')*)'")


def propose(sql, evidence, allowed_tables):
    event = {'status': 'no_supported_change', 'proof': False, 'changes': []}
    nodes = parse(sql, read='sqlite')
    if len(nodes) != 1 or not isinstance(nodes[0], exp.Select):
        return event
    tree = nodes[0]
    # Avoid scope, branch, join-predicate and aggregation ambiguities.
    if (len(list(tree.find_all(exp.Select))) != 1 or tree.args.get('with_')
            or tree.find(exp.Or) or tree.find(exp.Not)):
        return {**event, 'status': 'unsupported_structure'}
    allowed = {t.casefold() for t in allowed_tables}
    aliases = {}
    tables = list(tree.find_all(exp.Table))
    for t in tables:
        if t.name.casefold() not in allowed or t.db or t.catalog:
            return {**event, 'status': 'unsupported_structure'}
        key = t.alias_or_name.casefold()
        if key in aliases:
            return {**event, 'status': 'unsupported_structure'}
        aliases[key] = t.name.casefold()
    bindings = {}
    for m in _BINDING.finditer(evidence):
        key = (m[1].casefold(), m[2].casefold())
        if key[0] in allowed:
            bindings.setdefault(key, set()).add(m[3].replace("''", "'"))
    where = tree.args.get('where')
    if where is None:
        return event
    # Only top-level conjunction leaves; don't alter literals inside CASE/functions.
    def leaves(node):
        if isinstance(node, exp.Paren):
            return leaves(node.this)
        if isinstance(node, exp.And):
            return leaves(node.this) + leaves(node.expression)
        return [node]
    candidates = []
    for predicate in leaves(where.this):
        if not isinstance(predicate, exp.EQ):
            continue
        column, literal = predicate.this, predicate.expression
        if isinstance(literal, exp.Column):
            column, literal = literal, column
        if not isinstance(column, exp.Column) or not isinstance(literal, exp.Literal) or not literal.is_string:
            continue
        owner = aliases.get(column.table.casefold()) if column.table else (
            tables[0].name.casefold() if len(tables) == 1 else None)
        key = (owner, column.name.casefold())
        values = bindings.get(key, set())
        if len(values) == 1:
            candidates.append((key, literal, next(iter(values))))
    # Repeated filters on the same field need reasoning; do not guess.
    for key, literal, expected in candidates:
        if sum(k == key for k, _, _ in candidates) != 1 or literal.this == expected:
            continue
        event['changes'].append({'table': key[0], 'column': key[1],
                                 'old_value': literal.this, 'evidence_value': expected})
        literal.replace(exp.Literal.string(expected))
    if event['changes']:
        event.update(status='candidate', sql=tree.sql(dialect='sqlite'))
    return event


def verify_and_repair(service, state):
    policy = service.policies[state['database_id']]
    if policy.engine != 'sqlite' or not state.get('generated') or state['result'].get('truncated'):
        return {'semantic_repair': {'status': 'skipped', 'proof': False}}
    # Same explicit evidence boundary used by the benchmark; no inference from question words.
    evidence = state.get('question', '').partition('\nProvided BIRD evidence: ')[2]
    event = {'status': 'unavailable_original_retained', 'proof': False}
    try:
        event = propose(state['sql'], evidence, policy.tables)
        if event['status'] != 'candidate':
            return {'semantic_repair': event}
        # Re-check the repaired query before it can replace the original.
        checked = propose(event['sql'], evidence, policy.tables)
        if checked['status'] != 'no_supported_change':
            raise ValueError('explicit evidence recheck failed')
        result = service._query(state['database_id'], event['sql'])
        if result.get('truncated'):
            raise ValueError('repaired result truncated')
        event.update(status='repaired', original_sql=state['sql'], original_result=state['result'],
                     original_review=state.get('semantic_review', {}))
        return {'sql': event['sql'], 'result': result, 'semantic_repair': event,
                'allow_writes': False,
                'semantic_review': {'status': 'needs_review', 'proof': False,
                                    'reason': 'explicit_evidence_repair_not_independently_verified'}}
    except Exception as exc:
        event.update(status='unavailable_original_retained', error=str(exc)[:500])
        return {'semantic_repair': event}
