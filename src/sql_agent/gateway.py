"""Independent execution endpoint. Deploy with business write credentials only here."""
import json
import os
import secrets
import urllib.request

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan_id: str = Field(min_length=1, max_length=64)
    plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


def execute_remote(url, token, ident, fingerprint):
    if not token:
        raise ValueError('execution gateway token required')
    payload = json.dumps({'plan_id':ident,'plan_sha256':fingerprint}).encode()
    request = urllib.request.Request(url.rstrip('/')+'/execute',data=payload,
        headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value=json.loads(response.read(65536))
        if (value.get('id') != ident or value.get('proposal_sha256') != fingerprint
                or value.get('status') != 'completed'):
            raise ValueError('invalid execution gateway receipt')
        return value
    except Exception as exc:
        raise ValueError('gateway outcome unavailable; reconcile receipt before retry; no local fallback') from exc


def create_app(service=None, token=None):
    from .mutations import MutationService
    service = service if service is not None else MutationService.from_env()
    token = token if token is not None else os.getenv('SQL_AGENT_GATEWAY_TOKEN','')
    if service is None or not token:
        raise ValueError('gateway requires explicit mutation configuration and token')
    app=FastAPI(title='PeopleOps Copilot Execution Gateway')

    @app.get('/health')
    def health():
        return {'status':'ok'}

    @app.post('/execute')
    def execute(payload:ExecutionRequest, authorization:str|None=Header(default=None)):
        if not secrets.compare_digest(authorization or '', 'Bearer '+token):
            raise HTTPException(401,'gateway authentication required')
        try:
            body, reviewer, decision=service.get(payload.plan_id)
            if decision != 'approve' or not reviewer or body['proposal_sha256'] != payload.plan_sha256:
                raise ValueError('approved immutable plan required')
            # Reuse all hash, policy, expiry, transactional precondition and
            # UNKNOWN checks; caller cannot send SQL or choose the reviewer.
            return service._review(payload.plan_id,payload.plan_sha256,reviewer,'approve',local_execution=True)
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc
    return app
