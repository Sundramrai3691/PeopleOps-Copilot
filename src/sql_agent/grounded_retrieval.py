"""Opt-in field documentation and bounded value lookup for registered SQLite data.

Only catalog/CSV metadata and live database values enter the corpus. No examples
or evaluation answers are accepted. Retrieval misses leave generation unchanged.
"""
import csv
import json
import re
import sqlite3
import time
from pathlib import Path
from .retrieval import KnowledgeRetriever

STOP = set('the a an of in on for to from is are was were and or with which what how many much list show give find all each by as at than that their have has had using please return provided bird evidence'.split())


def terms(text):
    return {w for w in re.findall(r'[a-z0-9]+', text.lower()) if len(w)>2 and w not in STOP}


SQL_TERMS = {'max','min','avg','sum','count','select','where','null','between','cast','real','like'}


def split_request(text):
    question, separator, evidence = text.partition('\nProvided BIRD evidence: ')
    return question, evidence if separator else ''


def field_anchors(field):
    # Deliberately exclude table names and value-description examples: these
    # made County questions match MailCity simply because both belonged to schools.
    name = re.sub(r'([a-z])([A-Z])', r'\1 \2', field['column'])
    return terms(name + ' ' + field.get('semantic_name', ''))


def supplied_binding(field, evidence):
    column = re.escape(field['column'])
    identifier = r'(?<![\w])(?:[A-Za-z_]\w*\.)?[`"]?' + column + r'[`"]?(?![\w])'
    # Only explicit string equality/IN mappings. No inferred bindings from gold
    # or nearby quoted strings; uncertain cases remain eligible for lookup.
    pattern = identifier + r"\s*(?:=\s*['\"]|IN\s*\(\s*['\"])"
    return bool(re.search(pattern, evidence, re.I))


def lookup_phrases(question):
    quoted = re.findall(r"['\"]([^'\"]{1,80})['\"]", question)
    words = re.findall(r'\b[\w-]+\b', question)
    phrases = quoted + [' '.join(words[i:i+n]) for n in (3,2,1)
                       for i in range(len(words)-n+1)
                       if terms(' '.join(words[i:i+n]))]
    return list(dict.fromkeys(p.lower() for p in phrases
                if len(p)<=80 and p.lower() not in SQL_TERMS))[:80]


def complete_matches(values):
    # Prefer the full matched phrase over a matched fragment of that phrase.
    return [v for v in values if not any(v != other and
            (' '+v.lower()+' ') in (' '+other.lower()+' ') for other in values)]


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def field_documents(root, policies):
    documents=[]; fields=[]
    for did, policy in sorted(policies.items()):
        db=sqlite3.connect(policy.database.as_uri()+'?mode=ro',uri=True)
        try:
            for table in sorted(policy.tables):
                columns={r[1]:r[2] for r in db.execute('PRAGMA table_info('+quote(table)+')')}
                path=Path(root)/did/'database_description'/(table+'.csv')
                if not path.exists(): continue
                with path.open(encoding='utf-8-sig',errors='replace',newline='') as stream:
                    for index,row in enumerate(csv.DictReader(stream)):
                        row={str(k).strip():v for k,v in row.items() if k}
                        column=(row.get('original_column_name') or '').strip()
                        if column not in columns: continue
                        description='\n'.join(str(row.get(k) or '').strip() for k in ('column_name','column_description','value_description')).strip()
                        text=f'Table: {table}\nColumn: {column}\nType: {columns[column]}\n{description}'
                        # Oversize metadata is skipped, never silently cut mid-definition.
                        if not description or len(text)>12000: continue
                        ident=f'{did}:{table}:{index}'
                        document=dict(id=ident,database_id=did,title=f'{table}.{column}'[:200],text=text,
                                      tables=[table],source=str(path),version='field_metadata_v1')
                        documents.append(document)
                        fields.append(dict(document=document,table=table,column=column,type=columns[column],
                                           semantic_name=str(row.get('column_name') or ''),
                                           terms=terms(table+' '+column+' '+description)))
        finally: db.close()
    return documents,fields


class GroundedRetriever:
    def __init__(self, fields, policies, *, mode, model_path=None, cache_path=None, reranker_path=None):
        self.fields,self.policies,self.mode=fields,policies,mode
        self.model_path,self.cache_path,self.reranker_path=model_path,cache_path,reranker_path
        self.documents=[f['document'] for f in fields]

    def retrieve(self, database_id, question, allowed_tables, **_):
        user_question, evidence = split_request(question)
        query=terms(user_question)
        fields=[f for f in self.fields if f['document']['database_id']==database_id and f['table'] in allowed_tables]
        skipped=[]
        eligible=[]
        for field in fields:
            if supplied_binding(field,evidence):
                skipped.append(dict(field=field['document']['title'],reason='explicit_binding_already_provided'))
            elif query & field_anchors(field):
                eligible.append(field)
        candidates=sorted(eligible,key=lambda f:(-len(query & field_anchors(f)),f['document']['id']))[:12]
        hits=[]; probe_log=[]; retrieval={}
        if self.mode in ('field_docs','field_both') and candidates:
            retriever=KnowledgeRetriever([f['document'] for f in candidates],mode='hybrid',
                model_path=self.model_path,cache_path=self.cache_path,reranker_path=self.reranker_path,chunking='structure')
            retrieval=retriever.retrieve(database_id,question,allowed_tables,top_k=3,max_chars=2400)
            seen=set()
            for hit in retrieval['hits']:
                # Do not repeat passages already supplied in the request/evidence.
                normalized=' '.join(hit['text'].lower().split())
                if normalized in seen or normalized in ' '.join(question.lower().split()): continue
                seen.add(normalized);hits.append(hit)
        if self.mode in ('field_values','field_both') and candidates:
            values,probe_log=self.lookup(database_id,user_question,candidates[:8])
            hits.extend(values)
        # Whole passages only, max 3600 characters including live-value evidence.
        selected=[]; remaining=3600
        for hit in hits:
            if len(hit['text'])<=remaining:
                selected.append(hit);remaining-=len(hit['text'])
        return dict(method='gated_field_retrieval_v2',mode=self.mode,status='matched' if selected else 'no_match',
                    hits=selected,context_chars=3600-remaining,candidate_fields=[f['document']['title'] for f in candidates],
                    gate='explicit_field_anchor_and_no_supplied_binding',skipped_fields=skipped,probes=probe_log,
                    documentation_retrieval=retrieval)

    def lookup(self,did,question,candidates):
        policy=self.policies[did];start=time.monotonic();hits=[];events=[]
        db=sqlite3.connect(policy.database.as_uri()+'?mode=ro',uri=True)
        db.execute('PRAGMA query_only=ON')
        db.enable_load_extension(False)
        db.set_progress_handler(lambda:int(time.monotonic()-start>2),1000)
        allowed=set(policy.tables)
        def authorize(action,first,second,database,source):
            if database not in (None,'main') or source is not None: return sqlite3.SQLITE_DENY
            if action==sqlite3.SQLITE_SELECT or action==sqlite3.SQLITE_READ and first in allowed: return sqlite3.SQLITE_OK
            if action==sqlite3.SQLITE_FUNCTION and second in ('lower','length'): return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        db.set_authorizer(authorize)
        needles=lookup_phrases(question)
        try:
            for field in candidates:
                if time.monotonic()-start>2: break
                if not any(t in field['type'].upper() for t in ('TEXT','CHAR','CLOB')): continue
                column=quote(field['column']); table=quote(field['table'])
                event=dict(table=field['table'],column=field['column'])
                try:
                    if not needles: continue
                    sql=f'SELECT DISTINCT {column} FROM {table} WHERE length({column})<=120 AND lower({column}) IN ('+','.join('?' for _ in needles)+') ORDER BY length('+column+') DESC, '+column+' LIMIT 8'
                    rows=db.execute(sql,needles).fetchall()
                    event.update(status='matched' if rows else 'no_match',count=len(rows))
                    if rows:
                        values=complete_matches([r[0] for r in rows])[:4]
                        hits.append(dict(id=field['document']['id']+':values',tables=[field['table']],
                            text=f"Observed exact text matches for {field['table']}.{field['column']}: "+json.dumps(values),
                            source='live_readonly_database',database_id=did,match_policy='exact_case_insensitive'))
                except sqlite3.Error as exc:
                    event.update(status='unavailable',error=str(exc))
                events.append(event)
        finally:db.close()
        return hits,events
