"""Local stdio MCP adapter for the authenticated SQL API."""
from __future__ import annotations

import json
import os
import sys
from typing import Any
import urllib.request
from urllib.parse import quote

PROTOCOL_VERSION = "2024-11-05"
TOOL_SCHEMAS = [
    {"name": "list_sql_tasks", "description": "List registered SQL tasks and fixed output contracts.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "submit_sql_task", "description": "Submit a read-only SQL task or query repair for asynchronous execution.",
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "string"}, "question": {"type": "string"},
         "initial_sql": {"type": "string"}, "idempotency_key": {"type": "string"}},
         "required": ["task_id", "idempotency_key"], "additionalProperties": False}},
    {"name": "get_sql_job", "description": "Read job status, output and execution evidence.",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}},
                     "required": ["job_id"], "additionalProperties": False}},
]

# Deliberately no approval tool: model clients can inspect/propose, never approve.
TOOL_SCHEMAS += [
    {"name": "list_sql_sources", "description": "List configured analytics sources and task contracts.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "submit_sql_request", "description": "Submit a question or SQL through the asynchronous analytics workflow. Choose task_id OR database_id. Changes pause for human approval.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"},
         "database_id": {"type": "string"}, "question": {"type": "string"}, "sql": {"type": "string"},
         "idempotency_key": {"type": "string"}}, "required": ["idempotency_key"], "additionalProperties": False}},
    {"name": "get_sql_request", "description": "Read unified request state, SQL, output or approval proposal.",
     "inputSchema": {"type": "object", "properties": {"request_id": {"type": "string"}},
                     "required": ["request_id"], "additionalProperties": False}},
    {"name": "list_databases", "description": "List explicitly configured CRUD databases and table/DDL policies.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "query_database", "description": "Execute one read-only SELECT against a configured database.",
     "inputSchema": {"type": "object", "properties": {"database_id": {"type": "string"}, "sql": {"type": "string"}},
                     "required": ["database_id", "sql"], "additionalProperties": False}},
    {"name": "propose_database_change", "description": "Preview INSERT/UPDATE/DELETE/CREATE TABLE/DROP TABLE. Does NOT commit changes. A human administrator must approve through the browser/API.",
     "inputSchema": {"type": "object", "properties": {"database_id": {"type": "string"}, "sql": {"type": "string"}},
                     "required": ["database_id", "sql"], "additionalProperties": False}},
    {"name": "get_database_change", "description": "Inspect a stored change proposal and execution status.",
     "inputSchema": {"type": "object", "properties": {"proposal_id": {"type": "string"}},
                     "required": ["proposal_id"], "additionalProperties": False}},
]

# These tools call the PeopleOps service through FastAPI; each operation is a
# database boundary with authorization applied again inside the service.
TOOL_SCHEMAS += [
    {"name": "get_employee_profile", "description": "Read an authorized synthetic employee profile.", "inputSchema": {"type":"object","properties":{"employee_code":{"type":"string"}},"required":["employee_code"],"additionalProperties":False}},
    {"name": "get_leave_balance", "description": "Read an authorized employee's leave balances.", "inputSchema": {"type":"object","properties":{"employee_code":{"type":"string"}},"required":["employee_code"],"additionalProperties":False}},
    {"name": "get_team_headcount", "description": "Read department headcount; manager or HR access required.", "inputSchema": {"type":"object","properties":{"department":{"type":"string"}},"additionalProperties":False}},
    {"name": "get_attendance_summary", "description": "Read an authorized employee's last 90 days attendance summary.", "inputSchema": {"type":"object","properties":{"employee_code":{"type":"string"}},"required":["employee_code"],"additionalProperties":False}},
    {"name": "create_leave_request", "description": "Propose a validated leave request; the write waits for human approval.", "inputSchema": {"type":"object","properties":{"employee_code":{"type":"string"},"leave_type":{"type":"string","enum":["annual","sick"]},"start_date":{"type":"string"},"end_date":{"type":"string"},"reason":{"type":"string"}},"required":["employee_code","leave_type","start_date","end_date"],"additionalProperties":False}},
    {"name": "ask_hr_policy", "description": "Retrieve relevant synthetic HR policy passages with source citations.", "inputSchema": {"type":"object","properties":{"question":{"type":"string"}},"required":["question"],"additionalProperties":False}},
]


def api_call(path: str, payload: dict | None = None, idempotency_key: str | None = None):
    key = os.environ.get("SQL_AGENT_MCP_API_KEY")
    if not key:
        raise ValueError("SQL_AGENT_MCP_API_KEY is required")
    base = os.environ.get("SQL_AGENT_API_URL", "http://127.0.0.1:8000").rstrip("/")
    headers = {"x-api-key": key, "content-type": "application/json"}
    if idempotency_key:
        headers["idempotency-key"] = idempotency_key
    request = urllib.request.Request(base + path, headers=headers,
        data=None if payload is None else json.dumps(payload).encode())
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def dispatch(message: dict) -> dict | None:
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    if "id" not in message:
        return None
    request_id = message["id"]
    def result(data):
        return {"jsonrpc": "2.0", "id": request_id, "result": data}
    def error(code, text):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": text}}
    method = message.get("method")
    try:
        if method == "initialize":
            return result({"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": "peopleops-copilot", "version": "1.0.0"}})
        if method == "ping":
            return result({})
        if method == "tools/list":
            return result({"tools": TOOL_SCHEMAS})
        if method != "tools/call":
            return error(-32601, "Method not found")
        params = message.get("params")
        if not isinstance(params, dict):
            return error(-32602, "Invalid params: expected an object")
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(args, dict):
            return error(-32602, "Invalid arguments")
        if name == "list_sql_tasks" and not args:
            output = api_call("/v1/tasks")
        elif name == 'list_sql_sources' and not args:
            output = api_call('/v1/sources')
        elif name == 'submit_sql_request' and 'idempotency_key' in args and not set(args) - {'task_id', 'database_id', 'question', 'sql', 'idempotency_key'}:
            if not all(isinstance(v, str) and v for v in args.values()):
                return error(-32602, 'Arguments must be nonempty strings')
            output = api_call('/v1/requests', {k: v for k, v in args.items() if k != 'idempotency_key'}, args['idempotency_key'])
        elif name == 'get_sql_request' and set(args) == {'request_id'} and isinstance(args['request_id'], str):
            output = api_call('/v1/requests/' + quote(args['request_id'], safe=''))
        elif name == 'list_databases' and not args:
            output = api_call('/v1/databases')
        elif name in {'query_database', 'propose_database_change'} and set(args) == {'database_id', 'sql'}:
            if not all(isinstance(v, str) and v for v in args.values()):
                return error(-32602, 'Arguments must be nonempty strings')
            output = api_call('/v1/database-query' if name == 'query_database' else '/v1/mutations', args)
        elif name == 'get_database_change' and set(args) == {'proposal_id'} and isinstance(args['proposal_id'], str):
            output = api_call('/v1/mutations/' + quote(args['proposal_id'], safe=''))
        elif name in {"get_employee_profile", "get_leave_balance", "get_attendance_summary"} and set(args)=={"employee_code"} and isinstance(args["employee_code"],str):
            endpoint = {"get_employee_profile":"/employees/", "get_leave_balance":"/employees/", "get_attendance_summary":"/employees/"}[name]
            suffix = {"get_employee_profile":"", "get_leave_balance":"/leave-balance", "get_attendance_summary":"/attendance"}[name]
            output = api_call(endpoint + quote(args["employee_code"], safe="") + suffix)
        elif name == "get_team_headcount" and not set(args)-{"department"} and (not args or isinstance(args.get("department"),str)):
            output = api_call("/agent/query/headcount", args)
        elif name == "create_leave_request" and {"employee_code","leave_type","start_date","end_date"} <= set(args) and not set(args)-{"employee_code","leave_type","start_date","end_date","reason"}:
            if not all(isinstance(v,str) for v in args.values()): return error(-32602,"Arguments must be strings")
            output = api_call("/agent/actions/leave-requests", args)
        elif name == "ask_hr_policy" and set(args)=={"question"} and isinstance(args["question"],str):
            output = api_call("/agent/query/policy", args)
        elif name == "get_sql_job" and set(args) == {"job_id"} and isinstance(args["job_id"], str):
            output = api_call("/v1/jobs/" + quote(args["job_id"], safe=""))
        elif name == "submit_sql_task" and {"task_id", "idempotency_key"} <= set(args) and not set(args) - {"task_id", "idempotency_key", "question", "initial_sql"}:
            if not all(isinstance(v, str) and v for v in args.values()):
                return error(-32602, "Arguments must be nonempty strings")
            output = api_call("/v1/jobs", {k: v for k, v in args.items() if k != "idempotency_key"}, args["idempotency_key"])
        else:
            return error(-32602, "Unknown tool or invalid arguments")
        return result({"content": [{"type": "text", "text": json.dumps(output)}],
                       "structuredContent": output, "isError": False})
    except (KeyError, TypeError, ValueError, OSError) as exc:
        return result({"content": [{"type": "text", "text": str(exc)}], "isError": True})


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            response = dispatch(json.loads(line))
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
