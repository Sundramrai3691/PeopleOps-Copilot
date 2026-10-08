"""PeopleOps Copilot's shared graph for analysis, SQL reads, and approved changes."""
from contextlib import contextmanager
try:
    import fcntl
except ImportError:  # Windows local development
    fcntl = None
    import msvcrt
from pathlib import Path
import threading
from typing import TypedDict
import uuid

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class DatabaseState(TypedDict, total=False):
    id: str
    database_id: str
    sql: str
    submitted_by: str
    mode: str
    proposal: dict
    approval: dict
    result: dict
    question: str
    generated: bool
    attempt: int
    error: str
    allow_writes: bool
    request_sha256: str
    retrieval: dict
    clarification: dict | None
    clarification_rounds: int
    semantic_review: dict
    semantic_repair: dict
    schema_linking: dict
    rejected_proposals: list[dict]
    planner_calls: list[dict]


class DatabaseWorkflow:
    def __init__(self, service):
        self.service = service
        if service is None:
            return  # Stateless contracted-analysis invocation of the same graph.
        self.path = Path(str(service.store) + '.graph.sqlite')
        self.lock_dir = Path(str(service.store) + '.locks')
        self.lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    @contextmanager
    def session(self, ident, planner=None):
        # Local single-host locks, not a distributed workflow claim mechanism.
        lock_path = self.lock_dir / (ident + '.lock')
        with _PROCESS_LOCKS_GUARD:
            process_lock = _PROCESS_LOCKS.setdefault(str(lock_path.resolve()), threading.Lock())
        if not process_lock.acquire(blocking=False):
            raise ValueError('this request is already executing; inspect status and retry later')
        try:
          with lock_path.open('a+') as lock:
            try:
                if fcntl:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    lock.seek(0)
                    if not lock.read(1):
                        lock.write('0')
                        lock.flush()
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except (BlockingIOError, PermissionError) as exc:
                raise ValueError('this request is already executing; inspect status and retry later') from exc
            try:
                with SqliteSaver.from_conn_string(str(self.path)) as saver:
                    graph = self.graph(saver, planner=planner)
                    config = {'configurable': {'thread_id': ident}}
                    try:
                        yield graph, config
                    except Exception as exc:
                        # Read the last committed node, not the failed proposal.
                        # Preserve the original exception even if evidence cannot be read.
                        try:
                            exc.workflow_state = graph.get_state(config).values
                        except Exception:
                            pass
                        raise
            finally:
                if fcntl:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                else:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            process_lock.release()

    def graph(self, saver, analysis=None, planner=None):
        graph = StateGraph(DatabaseState)

        def validate(state):
            if state['mode'] == 'analysis':
                if analysis is None:
                    raise ValueError('analysis executor unavailable')
                return {}
            policy = self.service.policies.get(state['database_id'])
            if policy is None:
                raise ValueError('database not enabled')
            if state['mode'] == 'mutation':
                if policy.engine == 'postgresql':
                    from .postgres_mutations import statement
                    statement(state['sql'], policy)
                else:
                    from .mutations import validate_sql
                    validate_sql(state['sql'], policy)
            return {}

        def retrieve(state):
            if not state.get('question') or state.get('sql') or not hasattr(planner, 'retrieve'):
                return {'retrieval': {'method': 'bm25', 'status': 'not_requested', 'hits': []}}
            return {'retrieval': planner.retrieve(state['database_id'], state['question'])}

        def schema_link(state):
            if state.get('sql') or not hasattr(planner, 'link_schema'):
                return {'schema_linking': {'status': 'not_requested'}}
            return {'schema_linking': planner.link_schema(state['database_id'], state['question'],
                                                         state.get('retrieval', {}))}

        def plan(state):
            if state.get('sql') and not state.get('error'):
                return {'generated': False, 'attempt': 1}
            if planner is None or not state.get('question'):
                raise ValueError('a configured model is required for natural-language requests')
            if state.get('attempt', 0) >= 3:
                raise ValueError('global SQL proposal budget exhausted')
            kwargs = {'context': state.get('retrieval', {})} if hasattr(planner, 'retrieve') else {}
            if hasattr(planner, 'link_schema'):
                kwargs['schema_context'] = state.get('schema_linking')
            from .request_planner import ClarificationRequired
            call_start = len(getattr(planner, 'call_events', []))
            try:
                sql = planner(state['database_id'], state['question'], state.get('sql', ''), state.get('error', ''), **kwargs)
            except ClarificationRequired as exc:
                if state.get('clarification_rounds', 0) >= 2:
                    raise ValueError('clarification budget exhausted') from exc
                return {'clarification': {'id': str(uuid.uuid4()), 'question': str(exc)},
                        'planner_calls': [*state.get('planner_calls', []), *getattr(planner, 'call_events', [])[call_start:]],
                        'clarification_rounds': state.get('clarification_rounds', 0) + 1}
            from sqlglot import parse_one
            dialect = 'postgres' if self.service.policies[state['database_id']].engine == 'postgresql' else 'sqlite'
            normalized = parse_one(sql, read=dialect).sql(dialect=dialect, normalize=True, comments=False)
            if any(item['normalized_sql'] == normalized for item in state.get('rejected_proposals', [])):
                raise ValueError('repeated normalized proposal; repair stopped')
            return {'sql': sql, 'generated': True, 'attempt': state.get('attempt', 0) + 1, 'error': '',
                    'planner_calls': [*state.get('planner_calls', []), *getattr(planner, 'call_events', [])[call_start:]]}

        def clarify(state):
            value = interrupt({'type': 'user_clarification', **state['clarification']})
            if (not isinstance(value, dict) or value.get('id') != state['clarification']['id']
                    or not isinstance(value.get('answer'), str) or not value['answer'].strip()
                    or len(value['answer']) > 4000):
                raise ValueError('reply does not match the pending clarification')
            return {'question': state['question'] + '\nUser clarification (untrusted):\n' + value['answer'],
                    'clarification': None, 'sql': ''}

        def classify(state):
            from .sql_validation import is_read_query, parse_statement
            policy = self.service.policies[state['database_id']]
            node = parse_statement(
                state['sql'], 'postgres' if policy.engine == 'postgresql' else 'sqlite'
            )
            mode = 'query' if is_read_query(node) else 'mutation'
            if mode == 'mutation' and not state.get('allow_writes', True):
                raise ValueError('this request is read-only')
            if mode == 'mutation':
                if policy.engine == 'postgresql':
                    from .postgres_mutations import statement
                    statement(state['sql'], policy)
                else:
                    from .mutations import validate_sql
                    validate_sql(state['sql'], policy)
            return {'mode': mode}

        def read_query(state):
            import sqlite3
            try:
                return {'result': self.service._query(state['database_id'], state['sql']), 'error': ''}
            except Exception as exc:
                if isinstance(exc, sqlite3.DatabaseError) and (
                        getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_AUTH
                        or 'not authorized' in str(exc).lower()):
                    raise ValueError('read operation denied by database policy') from exc
                repairable = isinstance(exc, sqlite3.OperationalError) and any(
                    term in str(exc).lower() for term in ('no such column', 'syntax error', 'ambiguous column'))
                if type(exc).__module__.startswith('psycopg') and getattr(exc, 'sqlstate', None) in {'42703', '42601', '42702'}:
                    repairable = True
                if not repairable or not state.get('generated') or state.get('attempt', 1) >= 3:
                    raise
                from sqlglot import parse_one
                dialect = 'postgres' if self.service.policies[state['database_id']].engine == 'postgresql' else 'sqlite'
                from .repair_feedback import repair_feedback
                feedback = repair_feedback(self.service.policies[state['database_id']], state['sql'], exc)
                rejected = {'normalized_sql':parse_one(state['sql'], read=dialect).sql(
                    dialect=dialect, normalize=True, comments=False), 'reason':str(exc)[:1000],
                    'repair_feedback': feedback, 'attempt':state.get('attempt', 1)}
                return {'error': feedback, 'allow_writes': False,
                        'rejected_proposals': [*state.get('rejected_proposals', []), rejected]}

        def analyze(state):
            outcome = analysis()
            return {'result': {'status': outcome.status, 'result': outcome.result}}

        def verify_result(state):
            if planner is None or not hasattr(planner, 'verify_result'):
                return {'semantic_review': {'status': 'not_run', 'proof': False}}
            return {'semantic_review': planner.verify_result(
                state['database_id'], state.get('question', ''), state['result'],
                **({'context': state.get('retrieval', {})} if hasattr(planner, 'retrieve') else {}))}

        def preview(state):
            proposal = self.service._propose(state['database_id'], state['sql'], state['submitted_by'], ident=state['id'])
            return {'proposal': proposal}

        def semantic_repair(state):
            if getattr(planner, 'pv_verification_enabled', False):
                from .pv_verification import verify_and_repair
                return verify_and_repair(self.service, state)
            if not getattr(planner, 'semantic_repair_enabled', False):
                return {}
            from .semantic_repair import repair_once
            return repair_once(self.service, planner, state)

        def approval(state):
            value = interrupt({'type': 'mutation_approval', 'proposal': state['proposal']})
            if not isinstance(value, dict) or value.get('proposal_sha256') != state['proposal']['proposal_sha256']:
                raise ValueError('approval does not match preview')
            return {'approval': value}

        def execute(state):
            value = state['approval']
            return {'result': self.service._review(state['id'], value['proposal_sha256'], value['reviewer'], value['decision'])}

        graph.add_node('validate', validate)
        graph.add_node('retrieve_context', retrieve)
        graph.add_node('schema_linking', schema_link)
        graph.add_node('plan_sql', plan)
        graph.add_node('clarify', clarify)
        graph.add_node('classify_sql', classify)
        graph.add_node('analyze', analyze)
        graph.add_node('read_query', read_query)
        graph.add_node('verify_result', verify_result)
        graph.add_node('semantic_repair', semantic_repair)
        graph.add_node('preview', preview)
        graph.add_node('await_approval', approval)
        graph.add_node('execute_transaction', execute)
        graph.add_edge(START, 'validate')
        graph.add_conditional_edges('validate', lambda s: 'analyze' if s['mode'] == 'analysis' else 'retrieve_context')
        graph.add_edge('retrieve_context', 'schema_linking')
        graph.add_edge('schema_linking', 'plan_sql')
        graph.add_edge('analyze', END)
        graph.add_conditional_edges('plan_sql', lambda s: 'clarify' if s.get('clarification') else 'classify_sql')
        graph.add_edge('clarify', 'retrieve_context')
        graph.add_conditional_edges('classify_sql', lambda s: 'read_query' if s['mode'] == 'query' else 'preview')
        graph.add_conditional_edges('read_query', lambda s: 'plan_sql' if s.get('error') else 'verify_result')
        graph.add_edge('verify_result', 'semantic_repair')
        graph.add_edge('semantic_repair', END)
        graph.add_edge('preview', 'await_approval')
        graph.add_edge('await_approval', 'execute_transaction')
        graph.add_edge('execute_transaction', END)
        return graph.compile(checkpointer=saver)

    def start(self, database_id, sql, submitted_by='', mode='mutation'):
        ident = str(uuid.uuid4())
        with self.session(ident) as (graph, config):
            state = graph.invoke({'id': ident, 'database_id': database_id, 'sql': sql,
                                  'submitted_by': submitted_by, 'mode': mode,
                                  'allow_writes': mode != 'query'}, config)
            return state['result'] if mode == 'query' else state['proposal']

    def request(self, ident, database_id, sql='', question='', submitted_by='', planner=None, answers=()):
        from .mutations import digest
        fingerprint = digest({'database_id': database_id, 'sql': sql, 'question': question, 'submitted_by': submitted_by})
        with self.session(ident, planner=planner) as (graph, config):
            previous = graph.get_state(config)
            if previous.values:
                if previous.values.get('request_sha256') != fingerprint:
                    raise ValueError('request identity changed')
                for key, value in {'database_id': database_id,
                                   'submitted_by': submitted_by}.items():
                    if previous.values.get(key, '') != value:
                        raise ValueError('request identity changed')
                pending = previous.values.get('clarification')
                if pending and answers and answers[-1].get('id') == pending['id'] and any(
                        t.interrupts for t in previous.tasks):
                    return graph.invoke(Command(resume=answers[-1]), config)
                if (previous.values.get('result') and not previous.next) or any(t.interrupts for t in previous.tasks):
                    return previous.values
                return graph.invoke(None, config)
            return graph.invoke({'id': ident, 'database_id': database_id, 'sql': sql,
                'question': question, 'submitted_by': submitted_by, 'mode': 'auto', 'allow_writes': True,
                'request_sha256': fingerprint}, config)

    def resume(self, ident, proposal_sha256, reviewer, decision):
        # Look up the application-owned id before using it in a lock-file path.
        body, _, _ = self.service.get(ident)
        if str(uuid.UUID(ident)) != ident or body['proposal_sha256'] != proposal_sha256:
            raise ValueError('approval must match the exact preview hash')
        if decision not in {'approve', 'reject'}:
            raise ValueError('invalid decision')
        approval = dict(proposal_sha256=proposal_sha256, reviewer=reviewer, decision=decision)
        with self.session(ident) as (graph, config):
            state = graph.get_state(config)
            if not state.values:
                raise ValueError('workflow checkpoint missing; proposal cannot be executed')
            previous = state.values.get('approval')
            if previous is not None and previous != approval:
                raise ValueError('proposal already reviewed')
            if state.values.get('result'):
                return state.values['result']
            interrupted = any(task.interrupts for task in state.tasks)
            result = graph.invoke(Command(resume=approval) if interrupted else None, config)
            return result['result']
