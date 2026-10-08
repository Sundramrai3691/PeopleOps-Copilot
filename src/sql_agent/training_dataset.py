"""Offline source-labelled SQL corpus preparation, not human semantic validation."""
import hashlib
import json
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing
from sqlglot import exp, parse


def score_response(row, text):
    result = {'correct':False, 'strict_json':False, 'execution_succeeded':False}
    try:
        json.loads(text)
        result['strict_json'] = True
    except (ValueError, TypeError):
        pass
    try:
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
        query = json.loads(raw)['sql']
        actual = execute_read(row['sql_context'], query)
        expected = row['expected_rows']
        ordered = bool(parse(row['sql'], read='sqlite')[0].args.get('order'))
        result['correct'] = actual == expected if ordered else Counter(map(tuple,actual)) == Counter(map(tuple,expected))
        result['execution_succeeded'] = True
    except Exception as exc:
        result['error_type'] = type(exc).__name__
    return result


def execute_read(context, query):
    if len(context) > 20000 or len(query) > 10000:
        raise ValueError('fixture exceeds budget')
    setup = parse(context, read='sqlite')
    statements = parse(query, read='sqlite')
    if not setup or any(not isinstance(n, (exp.Create, exp.Insert)) for n in setup):
        raise ValueError('fixture only permits CREATE TABLE and INSERT')
    if any(isinstance(n, exp.Create) and (str(n.args.get('kind')).upper() != 'TABLE' or n.expression)
           for n in setup):
        raise ValueError('only explicit table schemas permitted')
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
        raise ValueError('one read query required')
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 20000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
        conn.execute('PRAGMA max_page_count=1024')
        deadline = time.monotonic() + 1
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        allowed_functions = {'sum','avg','count','min','max','abs','round','coalesce','ifnull',
            'nullif','lower','upper','length','substr','substring','strftime','date','datetime',
            'julianday','cast','like','trim','replace','total'}
        def authorize(action, a, b, db, source):
            if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA):
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION and (b or '').lower() not in allowed_functions:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        try:
            for node in setup:
                conn.execute(node.sql(dialect='sqlite'))
            conn.set_authorizer(None)
            conn.execute('PRAGMA query_only=ON')
            conn.set_authorizer(authorize)
            cursor = conn.execute(statements[0].sql(dialect='sqlite'))
            rows = cursor.fetchmany(201)
            if len(rows) > 200:
                raise ValueError('result exceeds budget')
            return json.loads(json.dumps(rows))
        except sqlite3.Error as exc:
            raise ValueError('unsupported or invalid SQL fixture') from exc


def prepare_dataset(records, *, limits):
    splits = {name:[] for name in ('train','dev','test')}
    seen_questions, seen_schemas = set(), set()
    rejected = 0
    for row in sorted(records, key=lambda r: hashlib.sha256(str(r['id']).encode()).hexdigest()):
        domain = row['domain']
        bucket = int(hashlib.sha256(domain.encode()).hexdigest()[:8],16) % 10
        split = 'test' if bucket < 2 else 'dev' if bucket == 2 else 'train'
        if len(splits[split]) >= limits[split]:
            continue
        question_key = re.sub(r'\s+', ' ', row['sql_prompt'].lower()).strip()
        try:
            schema = ';'.join(n.sql(dialect='sqlite', normalize=True, comments=False)
                              for n in parse(row['sql_context'], read='sqlite') if isinstance(n,exp.Create))
            schema_hash = hashlib.sha256(schema.encode()).hexdigest()
            if question_key in seen_questions or schema_hash in seen_schemas:
                continue
            result = execute_read(row['sql_context'], row['sql'])
            if not result:
                rejected += 1
                continue
        except Exception:
            rejected += 1
            continue
        seen_questions.add(question_key)
        seen_schemas.add(schema_hash)
        splits[split].append({**{key:row[key] for key in
            ('id','domain','sql_prompt','sql_context','sql')},
            'schema_sha256':schema_hash, 'expected_rows':result})
        if all(len(splits[k]) == limits[k] for k in splits):
            break
    return {'version':'source_sql_v1_domain_disjoint', 'splits':splits,
            'rejected':rejected, 'label_scope':'source SQL executed on source fixtures; not independently human-verified',
            'leakage_scope':'domain-disjoint and exact normalized-schema/question dedup; no paraphrase guarantee'}
