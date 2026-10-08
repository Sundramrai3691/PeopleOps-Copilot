# PeopleOps Copilot usage

## Start locally

```powershell
pip install -e '.[api,dev]'
$env:SQL_AGENT_API_KEYS='{"employee":{"name":"E002","role":"employee"},"manager":{"name":"E001","role":"manager"},"hr":{"name":"hr","role":"hr"}}'
uvicorn sql_agent.api:create_app --factory --reload
```

All routes accept an `x-api-key` header. The principal name must be the employee code for employee and manager scoped access.

## Policy question

```sh
curl -H 'x-api-key: employee' -H 'content-type: application/json' \
  -d '{"question":"How much annual leave do new employees receive?"}' \
  http://127.0.0.1:8000/agent/query/policy
```

The response includes matching policy text, policy ID, title, section, effective date, and `evidence_sufficient`. A missing match returns no evidence rather than a guessed policy answer.

## Workforce analytics

Manager, HR, and admin principals can call `POST /agent/query/headcount` with an optional department name. The response includes generated fixed SQL, referenced tables, filters, validation mode, authorization result, and a short result summary. The employee aggregate query is read-only and parameterized.

## Employee records

`GET /employees/{code}`, `/leave-balance`, and `/attendance` are limited to the employee, their direct manager where applicable, HR, or admin. Sample employee principal `E002` can read E002. Manager principal `E001` can read direct report E002.

## Leave approval

Submit a proposal with `POST /agent/actions/leave-requests`:

```json
{"employee_code":"E002","leave_type":"annual","start_date":"2026-12-10","end_date":"2026-12-11","reason":"Personal leave"}
```

The request checks employee identity, future dates, date range, leave type, balance, and manager assignment. A successful response contains `approval_id`, `status: pending_approval`, and the action preview. A direct manager can list pending approvals and review one using `POST /approvals/{approval_id}/review` with `{"decision":"approve"}` or `{"decision":"reject"}`. Approval is one transaction that creates the leave request, updates the balance, and stores a hash-based audit event.

## MCP client

Run the stdio server with API access configured:

```sh
SQL_AGENT_API_URL=http://127.0.0.1:8000 \
SQL_AGENT_MCP_API_KEY=hr \
peopleops-copilot-mcp
```

MCP tool schemas and calls are in `src/sql_agent/mcp_server.py`. Tool execution delegates to authenticated API routes and the PeopleOps service. The MCP server does not expose approval tools.

## Docker

Run `docker compose up --build`. Compose configures PostgreSQL for employee data and persistent SQL-agent jobs. Replace the demonstration credentials before sharing the service.
