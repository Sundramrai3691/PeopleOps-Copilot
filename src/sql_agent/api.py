"""Authenticated SQL task submission, evidence inspection and review."""
import time
import uuid
from pathlib import Path

from .backends import job_repository_from_env
from .metrics import HttpMetrics
from .replay import build_replay_payload, replay_guarantee
from .security import ApiKeyAuthorizer, BrowserSessions
from .sql_config import SQLTaskRegistry

def create_app(jobs=None, registry=None, authorizer=None, mutations=None):
    from fastapi import FastAPI, Header, HTTPException, Request, Response, Cookie
    from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse, RedirectResponse
    from pydantic import BaseModel, Field, ConfigDict

    jobs = jobs if jobs is not None else job_repository_from_env()
    registry = registry if registry is not None else SQLTaskRegistry.from_env()
    authorizer = authorizer if authorizer is not None else ApiKeyAuthorizer.from_env()
    sessions = BrowserSessions()
    metrics = HttpMetrics()
    from .mutations import MutationService
    mutations = mutations if mutations is not None else MutationService.from_env()
    if mutations is not None and getattr(jobs, 'path', None):
        job_path = Path(jobs.path).resolve()
        if job_path in {mutations.store, mutations.workflow.path} or any(
                getattr(p, 'database', None) == job_path for p in mutations.policies.values()):
            raise ValueError('job, mutation control, graph and business databases must be separate')
    app = FastAPI(title="PeopleOps Copilot", description="Explainable employee operations agent for safe HR analytics, policy retrieval, and approval-gated actions.", version="1.0.0")
    from .peopleops import PeopleOpsService
    peopleops = PeopleOpsService()

    class PolicyQuestion(BaseModel):
        model_config = ConfigDict(extra="forbid")
        question: str = Field(min_length=3, max_length=2000)

    class AgentQuestion(BaseModel):
        model_config = ConfigDict(extra="forbid")
        question: str = Field(min_length=3, max_length=2000)

    class HeadcountQuery(BaseModel):
        model_config = ConfigDict(extra="forbid")
        department: str | None = Field(default=None, max_length=100)

    class LeaveProposal(BaseModel):
        model_config = ConfigDict(extra="forbid")
        employee_code: str = Field(pattern="^E[0-9]{3}$")
        leave_type: str = Field(pattern="^(annual|sick)$")
        start_date: str = Field(pattern="^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
        end_date: str = Field(pattern="^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
        reason: str = Field(default="", max_length=500)

    class ApprovalDecision(BaseModel):
        model_config = ConfigDict(extra="forbid")
        decision: str = Field(pattern="^(approve|reject)$")

    class JobPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")
        task_id: str = Field(min_length=1, max_length=128)
        question: str | None = Field(default=None, min_length=1, max_length=4000)
        initial_sql: str = Field(default="", max_length=20000)

    class ReviewPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")
        decision: str = Field(pattern="^(approve|reject)$")
        notes: str = Field(default="", max_length=2000)
        proposal_sha256: str | None = Field(default=None, pattern='^[a-f0-9]{64}$')

    class RequestPayload(BaseModel):
        model_config = ConfigDict(extra='forbid')
        task_id: str | None = Field(default=None, min_length=1, max_length=128)
        database_id: str | None = Field(default=None, min_length=1, max_length=128)
        question: str | None = Field(default=None, min_length=1, max_length=4000)
        sql: str = Field(default='', max_length=20000)

    class LoginPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")
        api_key: str = Field(min_length=1, max_length=4096)

    class ResumePayload(BaseModel):
        model_config = ConfigDict(extra='forbid')
        clarification_id: str = Field(min_length=1, max_length=128)
        answer: str = Field(min_length=1, max_length=4000)

    class MutationPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")
        database_id: str = Field(min_length=1, max_length=128)
        sql: str = Field(min_length=1, max_length=20000)

    class MutationReviewPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")
        proposal_sha256: str = Field(pattern="^[a-f0-9]{64}$")
        decision: str = Field(pattern="^(approve|reject)$")

    def mutation_service():
        if mutations is None:
            raise HTTPException(403, "database changes are disabled; configure an explicit allowlist")
        return mutations

    def mutation_call(fn, *args):
        import sqlite3
        from sqlglot.errors import SqlglotError
        try:
            return fn(*args)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except SqlglotError as exc:
            raise HTTPException(422, 'Invalid or unsupported SQL syntax') from exc
        except sqlite3.Error as exc:
            raise HTTPException(422, "Database/workflow error; inspect proposal status before retrying") from exc
        except Exception as exc:
            # Optional PostgreSQL dependency; do not expose DSNs or raw server errors.
            try:
                import psycopg
            except ImportError:
                raise exc
            if isinstance(exc, psycopg.Error):
                raise HTTPException(422, "PostgreSQL could not complete the request; inspect proposal status before retrying") from exc
            raise

    @app.get('/v1/databases')
    def databases(x_api_key: str | None = Header(default=None),
                  peopleops_session: str | None = Cookie(default=None)):
        authorize(x_api_key, 'viewer', peopleops_session)
        return {"databases": [] if mutations is None else mutations.describe()}

    @app.post('/v1/database-query')
    def database_query(payload: MutationPayload, x_api_key: str | None = Header(default=None),
                       peopleops_session: str | None = Cookie(default=None)):
        authorize(x_api_key, 'viewer', peopleops_session)
        return mutation_call(mutation_service().query, payload.database_id, payload.sql)

    @app.post('/v1/mutations', status_code=201)
    def propose_mutation(payload: MutationPayload, x_api_key: str | None = Header(default=None),
                         peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, 'operator', peopleops_session)
        return mutation_call(mutation_service().propose, payload.database_id, payload.sql, principal.name)

    @app.get('/v1/mutations/{ident}')
    def get_mutation(ident: str, x_api_key: str | None = Header(default=None),
                     peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, 'viewer', peopleops_session)
        return owned_mutation(ident, principal)

    @app.post('/v1/mutations/{ident}/review')
    def review_mutation(ident: str, payload: MutationReviewPayload,
                        x_api_key: str | None = Header(default=None),
                        peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, 'admin', peopleops_session)
        return mutation_call(mutation_service().review, ident, payload.proposal_sha256,
                             principal.name, payload.decision)

    def authorize(key, role, session=None):
        try:
            return authorizer.authenticate(key, role) if key else sessions.authenticate(session, role)
        except PermissionError as exc:
            raise HTTPException(401 if "invalid or missing" in str(exc) else 403, str(exc)) from exc

    def get(job_id):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job

    def owned_job(job_id, principal):
        job = get(job_id)
        if (principal.role != 'admin'
                and job.payload.get('_submitted_by') != principal.name):
            raise HTTPException(404, "job not found")
        return job

    def owned_mutation(ident, principal):
        body = mutation_call(mutation_service().inspect, ident)
        if (principal.role != 'admin'
                and body.get('submitted_by') != principal.name):
            raise HTTPException(404, "mutation proposal not found")
        return body

    @app.middleware("http")
    async def observe(request: Request, call_next):
        trace = request.headers.get("x-request-id", "")
        request.state.trace_id = trace if trace and len(trace) <= 128 else str(uuid.uuid4())
        started = time.perf_counter()
        origin = request.headers.get('origin')
        if (request.method not in {'GET', 'HEAD', 'OPTIONS'} and request.cookies.get('peopleops_session')
                and origin and origin != str(request.base_url).rstrip('/')):
            return JSONResponse({'detail': 'cross-origin session request rejected'}, status_code=403)
        response = await call_next(request)
        metrics.observe(request.method, request.url.path, response.status_code, time.perf_counter() - started)
        response.headers["x-request-id"] = request.state.trace_id
        return response

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        from .unified_dashboard import render_unified_dashboard
        return HTMLResponse(render_unified_dashboard(), headers={"Cache-Control": "no-store"})

    @app.get('/database', response_class=HTMLResponse)
    def database_console():
        return RedirectResponse('/', status_code=307)

    @app.get('/benchmark', response_class=HTMLResponse, include_in_schema=False)
    def benchmark_report():
        raise HTTPException(404, 'no scored PeopleOps evaluation report is available')

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "peopleops_copilot"}

    @app.post("/agent/query/policy")
    def policy_query(payload: PolicyQuestion, x_api_key: str | None = Header(default=None), peopleops_session: str | None = Cookie(default=None)):
        authorize(x_api_key, "employee", peopleops_session)
        return peopleops.ask_policy(payload.question)

    @app.post("/agent/query")
    def agent_query(payload: AgentQuestion, x_api_key: str | None = Header(default=None), peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, "employee", peopleops_session)
        import re
        question = payload.question
        codes = re.findall(r"\bE[0-9]{3}\b", question.upper())
        normalized = question.casefold()
        try:
            if codes and any(term in normalized for term in ("balance", "leave days")):
                return {**peopleops.get_leave_balance(codes[0],principal.name,principal.role),"tools_used":["get_leave_balance"],"authorization":"allowed"}
            if codes and any(term in normalized for term in ("profile", "who is", "employee details")):
                return {**peopleops.get_employee_profile(codes[0],principal.name,principal.role),"tools_used":["get_employee_profile"],"authorization":"allowed"}
            if any(term in normalized for term in ("policy", "leave", "remote", "reimbursement", "work week", "promotion", "benefit", "misconduct")):
                result = peopleops.ask_policy(question)
                return {**result, "tools_used":["ask_hr_policy"],"authorization":"allowed"}
            if any(term in normalized for term in ("headcount", "how many employees", "count employees", "department")):
                department = next((name for name in ("Engineering","People","Finance","Operations","Sales","Customer Support") if name.casefold() in normalized), None)
                result = peopleops.get_team_headcount(department, principal.name, principal.role)
                return {**result, "tools_used":["get_team_headcount"]}
            return {"answer":"Please clarify whether you need a policy answer, a department headcount, or an employee record lookup.","validation":"clarification_required"}
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/employees/{employee_code}")
    def employee_profile(employee_code: str, x_api_key: str | None = Header(default=None), peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, "employee", peopleops_session)
        try:
            return peopleops.get_employee_profile(employee_code, principal.name, principal.role)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/employees/{employee_code}/leave-balance")
    def leave_balance(employee_code: str, x_api_key: str | None = Header(default=None), peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, "employee", peopleops_session)
        try:
            return peopleops.get_leave_balance(employee_code, principal.name, principal.role)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/agent/query/headcount")
    def headcount(payload: HeadcountQuery, x_api_key: str | None = Header(default=None), peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, "employee", peopleops_session)
        try:
            return peopleops.get_team_headcount(payload.department, principal.name, principal.role)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/employees/{employee_code}/attendance")
    def attendance(employee_code: str, x_api_key: str | None = Header(default=None), peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, "employee", peopleops_session)
        try:
            return peopleops.get_attendance_summary(employee_code, principal.name, principal.role)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/agent/actions/leave-requests", status_code=202)
    def create_leave(payload: LeaveProposal, x_api_key: str | None = Header(default=None), peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, "employee", peopleops_session)
        try:
            return peopleops.create_leave_request(payload.employee_code, payload.leave_type, payload.start_date, payload.end_date, payload.reason, principal.name, principal.role)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/approvals/pending")
    def pending_approvals(x_api_key: str | None = Header(default=None), peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, "manager", peopleops_session)
        if principal.role not in {"manager", "hr", "admin"}:
            raise HTTPException(403, "manager role required")
        with peopleops.connect() as db:
            rows = db.execute("SELECT id,actor,payload,status,created_at FROM action_proposals WHERE status='pending' ORDER BY created_at LIMIT 100").fetchall()
            approvals = []
            for row in rows:
                body = {**dict(row), "payload":__import__('json').loads(row['payload'])}
                if principal.role == "manager":
                    employee = db.execute("SELECT manager_id FROM employees WHERE employee_code=?", (body["payload"].get("employee_code"),)).fetchone()
                    manager = db.execute("SELECT id FROM employees WHERE employee_code=?", (principal.name,)).fetchone()
                    if not employee or not manager or employee["manager_id"] != manager["id"]:
                        continue
                approvals.append(body)
            return {"approvals":approvals}

    @app.post("/approvals/{approval_id}/review")
    def review_approval(approval_id: str, payload: ApprovalDecision, x_api_key: str | None = Header(default=None), peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, "manager", peopleops_session)
        if principal.role not in {"manager", "hr", "admin"}:
            raise HTTPException(403, "manager role required")
        try:
            return peopleops.review_leave_request(approval_id, principal.name, principal.role, payload.decision)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/audit/events")
    def audit_events(limit: int = 100, x_api_key: str | None = Header(default=None), peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, "hr", peopleops_session)
        if principal.role not in {"hr", "admin"}:
            raise HTTPException(403, "HR role required")
        if not 1 <= limit <= 200:
            raise HTTPException(422, "limit must be between 1 and 200")
        with peopleops.connect() as db:
            rows = db.execute("SELECT id,actor_id,action,tool_name,decision,input_hash,created_at FROM audit_events ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return {"events":[dict(row) for row in rows]}

    @app.post('/v1/auth/login')
    def login(payload: LoginPayload, request: Request, response: Response):
        principal = authorize(payload.api_key, 'viewer')
        try:
            token = sessions.create(principal)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        sessions.revoke(request.cookies.get('peopleops_session'))
        response.set_cookie('peopleops_session', token, httponly=True,
                            secure=request.url.scheme == 'https', samesite='strict',
                            max_age=sessions.ttl_seconds, path='/')
        response.headers['Cache-Control'] = 'no-store'
        return {'name': principal.name, 'role': principal.role}

    @app.get('/v1/auth/session')
    def session_status(response: Response, peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(None, 'viewer', peopleops_session)
        response.headers['Cache-Control'] = 'no-store'
        return {'name': principal.name, 'role': principal.role}

    @app.post('/v1/auth/logout')
    def logout(response: Response, peopleops_session: str | None = Cookie(default=None)):
        sessions.revoke(peopleops_session)
        response.delete_cookie('peopleops_session', path='/', httponly=True, samesite='strict')
        response.headers['Cache-Control'] = 'no-store'
        return {'signed_out': True}

    @app.get("/v1/tasks")
    def tasks(x_api_key: str | None = Header(default=None),
               peopleops_session: str | None = Cookie(default=None)):
        authorize(x_api_key, "viewer", peopleops_session)
        return {"tasks": registry.describe()}

    @app.get('/v1/sources')
    def sources(x_api_key: str | None = Header(default=None), peopleops_session: str | None = Cookie(default=None)):
        authorize(x_api_key, 'viewer', peopleops_session)
        return {'tasks': registry.describe(), 'databases': [] if mutations is None else mutations.describe()}

    @app.post('/v1/requests', status_code=202)
    def submit_request(payload: RequestPayload, request: Request,
                       idempotency_key: str = Header(min_length=1, max_length=256),
                       x_api_key: str | None = Header(default=None),
                       peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, 'operator', peopleops_session)
        if bool(payload.task_id) == bool(payload.database_id):
            raise HTTPException(422, 'select exactly one configured task or database')
        content = payload.model_dump()
        if payload.task_id:
            try:
                task = registry.get(payload.task_id)
                task.validate_question(payload.question)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            content.update(question=payload.question or task.question, initial_sql=payload.sql,
                           _execution_contract_sha256=task.contract.snapshot().sha256,
                           _business_context_sha256=task.context_sha256)
        else:
            service = mutation_service()
            policy = service.policies.get(payload.database_id)
            if policy is None or not (payload.question or payload.sql.strip()):
                raise HTTPException(422, 'configured database and question or SQL required')
            content['_database_policy_sha256'] = policy.fingerprint()
        content.update(_trace_id=request.state.trace_id, _submitted_by=principal.name)
        job, created = jobs.submit('sql_request', content, idempotency_key)
        if job.job_type != 'sql_request' or any(job.payload.get(k) != v for k, v in content.items() if k != '_trace_id'):
            raise HTTPException(409, 'idempotency key already used for different inputs')
        return {'job': job.to_dict(), 'created': created}

    @app.get('/v1/requests/{job_id}')
    def inspect_request(job_id: str, x_api_key: str | None = Header(default=None),
                        peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, 'viewer', peopleops_session)
        job = owned_job(job_id, principal)
        result = job.to_dict()
        if job.result and job.result.get('operation') == 'mutation':
            result['result'] = {**job.result, 'proposal': mutation_call(mutation_service().inspect, job_id)}
        return result

    @app.post('/v1/requests/{job_id}/resume', status_code=202)
    def resume_request(job_id: str, payload: ResumePayload,
                       idempotency_key: str = Header(min_length=1, max_length=256),
                       x_api_key: str | None = Header(default=None),
                       peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, 'operator', peopleops_session)
        original = get(job_id)
        if principal.role != 'admin' and original.payload.get('_submitted_by') != principal.name:
            raise HTTPException(403, 'only the request owner or an admin can answer')
        try:
            resumed = jobs.resume(job_id, payload.clarification_id, payload.answer,
                                  principal.name, idempotency_key)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {'job': resumed.to_dict()}

    @app.get("/v1/capabilities")
    def capabilities(x_api_key: str | None = Header(default=None),
               peopleops_session: str | None = Cookie(default=None)):
        authorize(x_api_key, "viewer", peopleops_session)
        return {"domain": "sql", "operations": ["read_only_query", "query_repair", "contract_validation"],
                "validation_scope": "structural_contract_not_semantic_proof", "tasks": registry.describe(),
                "database_changes": {"enabled": mutations is not None,
                    "operations": [] if mutations is None else ["select", "insert", "update", "delete", "create_table", "drop_table"],
                    "approval": "admin_required", "databases": [] if mutations is None else mutations.describe()}}

    @app.post("/v1/jobs", status_code=202)
    def submit(payload: JobPayload, request: Request,
               idempotency_key: str = Header(min_length=1, max_length=256),
               x_api_key: str | None = Header(default=None),
               peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, "operator", peopleops_session)
        try:
            task = registry.get(payload.task_id)
            task.validate_question(payload.question)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        content = payload.model_dump()
        content["question"] = content["question"] or task.question
        content.update(_trace_id=request.state.trace_id, _submitted_by=principal.name,
                       _execution_contract_sha256=task.contract.snapshot().sha256,
                       _business_context_sha256=task.context_sha256)
        job, created = jobs.submit("sql_analysis", content, idempotency_key)
        # A reused key is allowed only for the same task inputs.
        if any(job.payload.get(k) != content.get(k) for k in ("task_id", "question", "initial_sql", "_execution_contract_sha256", "_business_context_sha256")):
            raise HTTPException(409, "idempotency key already used for different inputs")
        return {"job": job.to_dict(), "created": created}

    @app.get("/v1/jobs/{job_id}")
    def job(job_id: str, x_api_key: str | None = Header(default=None),
               peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, "viewer", peopleops_session)
        return owned_job(job_id, principal).to_dict()

    @app.get("/v1/jobs/{job_id}/events")
    def events(job_id: str, x_api_key: str | None = Header(default=None),
               peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, "viewer", peopleops_session)
        owned_job(job_id, principal)
        return {"events": jobs.events(job_id)}

    @app.get("/v1/traces/{trace_id}")
    def trace(trace_id: str, x_api_key: str | None = Header(default=None),
               peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, "viewer", peopleops_session)
        runs = jobs.find_by_trace_id(trace_id)
        if principal.role != 'admin':
            runs = [job for job in runs if job.payload.get('_submitted_by') == principal.name]
        if not runs:
            raise HTTPException(404, "trace not found")
        return {"trace_id": trace_id, "runs": [{"job": j.to_dict(), "events": jobs.events(j.job_id)} for j in runs]}

    @app.post("/v1/jobs/{job_id}/replay", status_code=202)
    def replay(job_id: str, request: Request,
               idempotency_key: str = Header(min_length=1, max_length=256),
               x_api_key: str | None = Header(default=None),
               peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, "operator", peopleops_session)
        original = owned_job(job_id, principal)
        try:
            content = build_replay_payload(original, request.state.trace_id, principal.name)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        job, created = jobs.submit("sql_analysis", content, idempotency_key, original.max_attempts)
        if job.payload.get("_replay_of_job_id") != job_id:
            raise HTTPException(409, "idempotency key already used for another request")
        return {"job": job.to_dict(), "created": created, "guarantee": replay_guarantee(original)}

    @app.post('/v1/requests/{job_id}/review')
    @app.post("/v1/jobs/{job_id}/review")
    def review(job_id: str, payload: ReviewPayload, x_api_key: str | None = Header(default=None),
               peopleops_session: str | None = Cookie(default=None)):
        principal = authorize(x_api_key, "admin", peopleops_session)
        original = get(job_id)
        if original.result and original.result.get('operation') == 'mutation':
            if not payload.proposal_sha256:
                raise HTTPException(422, 'exact proposal hash required for a database change')
            mutation_result = mutation_call(mutation_service().review, job_id,
                                            payload.proposal_sha256, principal.name, payload.decision)
            current = get(job_id)
            if current.status == 'needs_review':
                try:
                    current = jobs.review(job_id, principal.name, payload.decision, notes=payload.notes)
                except RuntimeError as exc:
                    raise HTTPException(409, 'review publication changed; inspect request status') from exc
            return {**current.to_dict(), 'mutation_result': mutation_result}
        try:
            return jobs.review(job_id, principal.name, payload.decision, notes=payload.notes).to_dict()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/v1/operations")
    def operations(x_api_key: str | None = Header(default=None),
               peopleops_session: str | None = Cookie(default=None)):
        authorize(x_api_key, "viewer", peopleops_session)
        return {"job_status_counts": jobs.status_counts()}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metric_text(x_api_key: str | None = Header(default=None),
               peopleops_session: str | None = Cookie(default=None)):
        authorize(x_api_key, "viewer", peopleops_session)
        return metrics.render(jobs.status_counts())

    return app
