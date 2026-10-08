"""Conservative table-level schema selection, not semantic join inference."""
import hashlib
import json
from .retrieval import tokens


def link_schema(catalog, question, knowledge, *, max_tables=8, max_chars=16000):
    if not 1 <= max_tables <= 64 or not 100 <= max_chars <= 200000:
        raise ValueError('invalid schema budget')
    terms = set(tokens(question))
    required = {table for hit in (knowledge or {}).get('hits', []) for table in hit['tables']}
    required |= {row[0] for row in catalog if row[0].lower() in terms}
    available = {row[0] for row in catalog}
    if required - available:
        raise ValueError('knowledge references unavailable schema')
    if len(required) > max_tables:
        raise ValueError('required schema exceeds table budget; narrow the request')
    ranked = sorted(catalog, key=lambda row: (
        row[0] not in required,
        -len(terms & set(tokens(json.dumps(row, default=str)))), row[0]))
    if len(catalog) > max_tables and not required and not any(
            terms & set(tokens(json.dumps(row, default=str))) for row in catalog):
        raise ValueError('schema evidence insufficient; name the relevant tables')
    selected = ranked[:max_tables]
    if len(json.dumps(selected, default=str)) > max_chars:
        raise ValueError('selected schema exceeds context budget; narrow the request')
    return {'method':'authorized_lexical_table_selection_v1',
            'schema':selected, 'selected_tables':[r[0] for r in selected],
            'catalog_tables':len(catalog), 'max_tables':max_tables,
            'schema_sha256':hashlib.sha256(json.dumps(catalog, sort_keys=True, default=str).encode()).hexdigest(),
            'column_policy':'all_columns_of_selected_tables',
            'join_policy':'no_inferred_join_paths'}
