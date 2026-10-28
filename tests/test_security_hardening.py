"""Regression tests for PeopleOps Copilot security hardening.

Validates:
1. Leave-approval separation of duties.
2. MCP identity propagation and least-privilege enforcement.
3. Security audit logging of authorization denials.
"""
from datetime import date, timedelta
import io
import json
from pathlib import Path
from urllib.error import HTTPError
import pytest
from fastapi.testclient import TestClient

from sql_agent.api import create_app
from sql_agent.jobs import SqliteJobRepository
from sql_agent.mcp_server import dispatch
from sql_agent.peopleops import PeopleOpsService
from sql_agent.security import ApiKeyAuthorizer, Principal
from sql_agent.sql_config import SQLTaskRegistry


@pytest.fixture
def people_service(tmp_path):
    return PeopleOpsService(str(tmp_path / "people.sqlite"))


# =====================================================================
# 1. Leave-Approval Separation of Duties Tests
# =====================================================================

def test_legitimate_employee_leave_workflow_succeeds(people_service):
    """An employee submits a proposal and their direct manager approves it."""
    start = date.today() + timedelta(days=14)
    end = start + timedelta(days=1)
    # E002 submits leave for themselves
    proposal = people_service.create_leave_request(
        "E002", "annual", start.isoformat(), end.isoformat(),
        "Vacation", "E002", "employee"
    )
    assert proposal["approval_required"] is True
    assert proposal["status"] == "pending_approval"
    approval_id = proposal["approval_id"]

    # E001 is direct manager of E002 (dept 1)
    result = people_service.review_leave_request(approval_id, "E001", "manager", "approve")
    assert result["status"] == "approved"
    assert result["request_id"].startswith("LR-")

    with people_service.connect() as db:
        prop = db.execute("SELECT status FROM action_proposals WHERE id=?", (approval_id,)).fetchone()
        assert prop["status"] == "approved"


def test_unrelated_manager_cannot_approve_leave(people_service):
    """A manager from another department cannot approve a subordinate outside their tree."""
    start = date.today() + timedelta(days=14)
    end = start + timedelta(days=1)
    proposal = people_service.create_leave_request(
        "E002", "annual", start.isoformat(), end.isoformat(),
        "Doctor appointment", "E002", "employee"
    )
    approval_id = proposal["approval_id"]

    # E021 is manager of Department 2 (People), E002 is in Department 1 (Engineering)
    with pytest.raises(PermissionError, match="direct manager"):
        people_service.review_leave_request(approval_id, "E021", "manager", "approve")

    # State must remain pending and balance untouched
    with people_service.connect() as db:
        prop = db.execute("SELECT status FROM action_proposals WHERE id=?", (approval_id,)).fetchone()
        assert prop["status"] == "pending"


def test_manager_initiator_cannot_self_approve(people_service):
    """Separation of duties: A manager who initiates a proposal cannot approve it."""
    start = date.today() + timedelta(days=15)
    end = start + timedelta(days=1)

    # Manager E001 creates proposal on behalf of subordinate E002
    proposal = people_service.create_leave_request(
        "E002", "annual", start.isoformat(), end.isoformat(),
        "Conference attendance", "E001", "manager"
    )
    approval_id = proposal["approval_id"]

    # Initial balance check
    bal_before = people_service.get_leave_balance("E002", "E001", "manager")["balances"]

    # E001 attempts to approve their own proposal -> Must fail separation of duties
    with pytest.raises(PermissionError, match="initiator|separation of duties"):
        people_service.review_leave_request(approval_id, "E001", "manager", "approve")

    # Verify balance was NOT mutated and status remained pending
    bal_after = people_service.get_leave_balance("E002", "E001", "manager")["balances"]
    assert bal_before == bal_after

    with people_service.connect() as db:
        prop = db.execute("SELECT status FROM action_proposals WHERE id=?", (approval_id,)).fetchone()
        assert prop["status"] == "pending"

    # An independent authorized approver (e.g. HR) CAN approve it
    hr_result = people_service.review_leave_request(approval_id, "hr_admin", "hr", "approve")
    assert hr_result["status"] == "approved"

    with people_service.connect() as db:
        prop = db.execute("SELECT status FROM action_proposals WHERE id=?", (approval_id,)).fetchone()
        assert prop["status"] == "approved"


def test_hr_initiator_cannot_self_approve(people_service):
    """Separation of duties: An HR initiator cannot approve their own proposal."""
    start = date.today() + timedelta(days=20)
    end = start + timedelta(days=1)

    # HR creates proposal for E002
    proposal = people_service.create_leave_request(
        "E002", "annual", start.isoformat(), end.isoformat(),
        "HR administrative leave", "hr_initiator", "hr"
    )
    approval_id = proposal["approval_id"]

    # hr_initiator attempts to approve their own proposal
    with pytest.raises(PermissionError, match="initiator|separation of duties"):
        people_service.review_leave_request(approval_id, "hr_initiator", "hr", "approve")

    # Independent HR approver can complete it
    independent_result = people_service.review_leave_request(approval_id, "hr_approver2", "hr", "approve")
    assert independent_result["status"] == "approved"


# =====================================================================
# 2. Security Audit Logging Tests
# =====================================================================

def test_audit_logs_unauthorized_employee_data_access(people_service):
    """Cross-employee data read denial is audited with denied decision."""
    with pytest.raises(PermissionError):
        people_service.get_employee_profile("E003", "E002", "employee")

    with people_service.connect() as db:
        events = db.execute(
            "SELECT actor_id, action, decision, input_hash FROM audit_events WHERE actor_id='E002'"
        ).fetchall()
        assert len(events) >= 1
        denied_event = events[-1]
        assert denied_event["decision"] == "denied"
        # Input hash must be a SHA-256 hex string (64 characters)
        assert len(denied_event["input_hash"]) == 64


def test_audit_logs_unauthorized_leave_review_by_employee(people_service):
    """An employee attempting to review leave requests must be denied and audited."""
    start = date.today() + timedelta(days=10)
    end = start + timedelta(days=1)
    proposal = people_service.create_leave_request(
        "E002", "annual", start.isoformat(), end.isoformat(),
        "Trip", "E002", "employee"
    )
    approval_id = proposal["approval_id"]

    # E003 (employee) tries to review approval
    with pytest.raises(PermissionError, match="manager"):
        people_service.review_leave_request(approval_id, "E003", "employee", "approve")

    with people_service.connect() as db:
        events = db.execute(
            "SELECT actor_id, action, decision FROM audit_events WHERE actor_id='E003' AND decision='denied'"
        ).fetchall()
        assert len(events) >= 1
        assert events[-1]["action"] == "approval_authorization"


def test_audit_logs_unauthorized_team_analytics(people_service):
    """An employee attempting to query department headcount must be denied and audited."""
    with pytest.raises(PermissionError, match="manager"):
        people_service.get_team_headcount("Engineering", "E002", "employee")

    with people_service.connect() as db:
        events = db.execute(
            "SELECT actor_id, action, decision FROM audit_events WHERE actor_id='E002' AND action='team_headcount_authorization'"
        ).fetchall()
        assert len(events) >= 1
        assert events[-1]["decision"] == "denied"


def test_audit_logs_privacy_no_plaintext_secrets(people_service):
    """Audit logs must store SHA-256 digests of inputs, not plaintext payloads."""
    start = date.today() + timedelta(days=12)
    sensitive_reason = "Confidential medical appointment xyz-987"
    people_service.create_leave_request(
        "E002", "sick", start.isoformat(), start.isoformat(),
        sensitive_reason, "E002", "employee"
    )

    with people_service.connect() as db:
        events = db.execute("SELECT * FROM audit_events").fetchall()
        for ev in events:
            # Sensitive reason should never appear in audit_events table
            row_text = " ".join(str(v) for v in ev)
            assert sensitive_reason not in row_text


# =====================================================================
# 3. MCP Identity Propagation & Least-Privilege Tests
# =====================================================================

def test_mcp_fails_closed_when_api_key_missing(monkeypatch):
    """MCP server fails closed with an error if no API key is configured."""
    monkeypatch.delenv("SQL_AGENT_MCP_API_KEY", raising=False)
    msg = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_employee_profile", "arguments": {"employee_code": "E002"}}
    }
    res = dispatch(msg)
    assert res["result"]["isError"] is True
    assert "SQL_AGENT_MCP_API_KEY is required" in res["result"]["content"][0]["text"]


def test_mcp_enforces_least_privilege_with_employee_key(tmp_path, monkeypatch):
    """An MCP server operating with an employee's API key enforces server-side RBAC."""
    monkeypatch.setenv("PEOPLEOPS_DATABASE", str(tmp_path / "mcp.sqlite"))
    app = create_app(
        jobs=SqliteJobRepository(Path(tmp_path / "jobs.sqlite")),
        registry=SQLTaskRegistry(),
        authorizer=ApiKeyAuthorizer({
            "key-e002": Principal("E002", "employee"),
            "key-e001": Principal("E001", "manager"),
            "key-hr": Principal("hr_user", "hr"),
        })
    )

    client = TestClient(app)

    def mock_urlopen(request, timeout=15):
        path = request.full_url.replace("http://127.0.0.1:8000", "")
        headers = dict(request.headers)
        data = request.data
        method = request.get_method()
        if method == "GET":
            response = client.get(path, headers=headers)
        elif method == "POST":
            json_body = json.loads(data.decode("utf-8")) if data else None
            response = client.post(path, headers=headers, json=json_body)
        else:
            raise NotImplementedError(method)

        if response.status_code >= 400:
            fp = io.BytesIO(response.content)
            raise HTTPError(request.full_url, response.status_code, response.text, response.headers, fp)
        res_io = io.BytesIO(response.content)
        return res_io

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    monkeypatch.setenv("SQL_AGENT_MCP_API_KEY", "key-e002")

    # 1. Employee E002 accessing their own profile -> 200 OK
    res_self = dispatch({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "get_employee_profile", "arguments": {"employee_code": "E002"}}
    })
    assert res_self["result"]["isError"] is False
    profile = json.loads(res_self["result"]["content"][0]["text"])
    assert profile["employee_code"] == "E002"

    # 2. Employee E002 attempting to access peer E003 profile -> Fails closed with error
    res_peer = dispatch({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "get_employee_profile", "arguments": {"employee_code": "E003"}}
    })
    assert res_peer["result"]["isError"] is True
    assert "403" in res_peer["result"]["content"][0]["text"]

    # 3. Employee E002 attempting to query department headcount -> Fails closed with error
    res_headcount = dispatch({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "get_team_headcount", "arguments": {"department": "Engineering"}}
    })
    assert res_headcount["result"]["isError"] is True
    assert "403" in res_headcount["result"]["content"][0]["text"]

    # 4. Caller attempts to inject extra role parameters -> Schema validation error -32602
    res_tamper = dispatch({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "get_employee_profile", "arguments": {"employee_code": "E003", "role": "admin"}}
    })
    assert res_tamper["error"]["code"] == -32602
