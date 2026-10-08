"""Conservative cardinality proof for registered additive measures, not SQL semantics.

Supports direct table scopes, SUM(column), and many-to-one joins proven from
live single-column, non-null primary keys. Unsupported shapes fail closed.
No data-value coincidence is used as a uniqueness guarantee.
"""
from sqlglot import parse, exp


def verify_grain(connection, sql, measures):
    def unknown(reason):
        return {'status': 'unproven', 'reason': reason, 'proof_scope': 'join_cardinality_only'}
    try:
        statements=parse(sql,read='sqlite')
        if len(statements)!=1 or not isinstance(statements[0],exp.Select):
            return unknown('unsupported_query_shape')
        tree=statements[0]
        if tree.find(exp.Union) or tree.find(exp.Window) or tree.find(exp.CTE):
            return unknown('unsupported_query_shape')
        declared={(t.lower(),c.lower()):k.lower() for t,c,k in measures}
        schemas={}
        def schema(table):
            if table not in schemas:
                escaped=table.replace('"','""')
                rows=connection.execute(f'PRAGMA table_info("{escaped}")').fetchall()
                primary=[r for r in rows if r[5]]
                unique=set()
                indexes=connection.execute(f'PRAGMA index_list("{escaped}")').fetchall()
                # INTEGER PRIMARY KEY DESC uses a nullable PK index, not rowid.
                rowid_key=(len(primary)==1 and primary[0][2].upper()=='INTEGER'
                           and not any(index[3]=='pk' for index in indexes))
                if len(primary)==1 and (primary[0][3] or rowid_key):
                    unique.add(primary[0][1].lower())
                schemas[table]=({r[1].lower() for r in rows},unique)
            return schemas[table]
        for (table,column),key in declared.items():
            columns,unique=schema(table)
            if column not in columns or key not in unique:
                return unknown('registered_grain_not_proven_by_live_primary_key')
        seen=set(); checked=0
        for scope in tree.find_all(exp.Select):
            source=scope.args.get('from_')
            if source is None or not isinstance(source.this,exp.Table):
                return unknown('unsupported_source')
            joins=scope.args.get('joins') or []
            relations=[source.this]+[j.this for j in joins]
            if any(not isinstance(t,exp.Table) or t.db or t.catalog for t in relations):
                return unknown('derived_or_external_source_not_supported')
            aliases={t.alias_or_name.lower():t.name.lower() for t in relations}
            if len(aliases)!=len(relations):return unknown('ambiguous_relation_alias')
            aggregates=[a for a in scope.find_all(exp.AggFunc) if a.find_ancestor(exp.Select) is scope]
            if joins and not aggregates:return unknown('joined_projection_grain_not_declared')
            edges=[]
            for join in joins:
                if join.args.get('side') not in (None,'','LEFT') or join.args.get('method') or join.args.get('using'):
                    return unknown('unsupported_join_shape')
                on=join.args.get('on')
                if on is None or on.find(exp.Or) or on.find(exp.Select):return unknown('unsupported_join_predicate')
                # Only conjunctions are admitted; do not infer equality through NOT.
                terms=list(on.flatten()) if isinstance(on,exp.And) else [on]
                for term in terms:
                    if not isinstance(term,exp.EQ):return unknown('unsupported_join_predicate')
                    a,b=term.this,term.expression
                    if isinstance(a,exp.Column) and isinstance(b,exp.Column):
                        if not a.table or not b.table:return unknown('ambiguous_join_column')
                        edges.append((a.table.lower(),a.name.lower(),b.table.lower(),b.name.lower()))
            for aggregate in aggregates:
                if not isinstance(aggregate,exp.Sum) or not isinstance(aggregate.this,exp.Column):
                    return unknown('unsupported_measure_expression')
                col=aggregate.this
                alias=col.table.lower() if col.table else next(iter(aliases)) if len(aliases)==1 else ''
                if alias not in aliases:return unknown('ambiguous_measure_source')
                measure=(aliases[alias],col.name.lower())
                if measure not in declared:return unknown('unregistered_measure')
                reached={alias}
                while True:
                    previous=set(reached)
                    for a,ac,b,bc in edges:
                        for old,new,key in ((a,b,bc),(b,a,ac)):
                            if old in reached and new in aliases and key in schema(aliases[new])[1]:reached.add(new)
                    if reached==previous:break
                if reached!=set(aliases):return unknown('join_can_repeat_measure_rows')
                seen.add(measure);checked+=1
        if seen!=set(declared):return unknown('registered_measure_missing')
        return {'status':'proven','reason':'registered_sum_rows_not_multiplied',
                'proof_scope':'join_cardinality_only','aggregates_checked':checked}
    except Exception as exc:
        return unknown('grain_check_unavailable:'+type(exc).__name__)
