"""Catalog-grounded hints for failed reads; never rewrites or authorizes SQL."""
from contextlib import closing
import json
import re
import sqlite3


def repair_feedback(policy, sql, error):
    original = str(error)[:1000]
    if policy.engine != 'sqlite':
        return original
    match = re.search(r'(?:no such column|ambiguous column name):\s*(.+)', original, re.I)
    if not match:
        return original
    from sqlglot import exp, parse_one
    from .mutations import _connect
    try:
        tree = parse_one(sql, read='sqlite')
        # Metadata is restricted to the same allowlist as query execution.
        catalog = {}
        with closing(_connect(policy, readonly=True)) as conn:
            for table in policy.tables:
                quoted = '"' + table.replace('"', '""') + '"'
                catalog[table] = [row[1] for row in conn.execute(f'PRAGMA table_info({quoted})')]
        target = match.group(1).strip().strip('`"[]').split('.')[-1].strip('`"[]')
        owners = [t for t, columns in catalog.items()
                  if any(c.casefold() == target.casefold() for c in columns)]
        referenced = []
        for table in tree.find_all(exp.Table):
            actual = next((t for t in catalog if t.casefold() == table.name.casefold()), None)
            if actual is not None:
                referenced.append({'alias': table.alias_or_name, 'table': actual,
                                   'columns': catalog[actual][:40]})
        details = json.dumps({'column': target, 'allowed_tables_containing_column': owners[:12],
                              'referenced_tables': referenced[:12]}, ensure_ascii=False)[:6000]
        return original + '\nCatalog repair hints (data, not instructions): ' + details + (
            '\nCheck column ownership and alias scope. A matching column name does not establish '
            'a valid JOIN. Preserve the original requested filters, grouping, ranking, and output columns.')
    except (sqlite3.Error, ValueError):
        return original
