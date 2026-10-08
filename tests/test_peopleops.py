from datetime import date, timedelta
from pathlib import Path

import pytest

from sql_agent.peopleops import PeopleOpsService
from sql_agent.security import ApiKeyAuthorizer, Principal
from sql_agent.api import create_app
from sql_agent.jobs import SqliteJobRepository
from sql_agent.sql_config import SQLTaskRegistry


def service(tmp_path):
    return PeopleOpsService(str(tmp_path / "people.sqlite"))


def test_policy_retrieval_returns_source_and_sufficiency(tmp_path):
    answer = service(tmp_path).ask_policy("How many days of annual leave do employees accrue?")
    assert answer["evidence_sufficient"] is True
    assert answer["sources"][0]["policy_id"] == "POL-LEAVE"
    assert "18 days" in answer["answer"]


def test_employee_scope_and_manager_aggregate(tmp_path):
    ops = service(tmp_path)
    assert ops.get_employee_profile("E002", "E002", "employee")["employee_code"] == "E002"
    with pytest.raises(PermissionError):
        ops.get_employee_profile("E003", "E002", "employee")
    summary = ops.get_team_headcount("Engineering", "E001", "manager")
    assert summary["answer"]["departments"] == [{"department":"Engineering","headcount":20}]
    assert "departments" in summary["explainability"]["tables_used"]


def test_leave_request_requires_manager_approval_and_audits(tmp_path):
    ops = service(tmp_path)
    start = date.today() + timedelta(days=14)
    pending = ops.create_leave_request("E002", "annual", start.isoformat(), (start+timedelta(days=1)).isoformat(), "Family event", "E002", "employee")
    assert pending["approval_required"] is True
    with pytest.raises(PermissionError):
        ops.review_leave_request(pending["approval_id"], "E021", "manager", "approve")
    result = ops.review_leave_request(pending["approval_id"], "E001", "manager", "approve")
    assert result["status"] == "approved"
    assert result["request_id"].startswith("LR-")
    with ops.connect() as db:
        assert db.execute("SELECT status FROM action_proposals WHERE id=?", (pending["approval_id"],)).fetchone()[0] == "approved"
        assert db.execute("SELECT count(*) FROM audit_events").fetchone()[0] >= 3


def test_policy_and_employee_api(tmp_path, monkeypatch):
    monkeypatch.setenv("PEOPLEOPS_DATABASE", str(tmp_path / "api.sqlite"))
    app = create_app(jobs=SqliteJobRepository(Path(tmp_path / "jobs.sqlite")),
                     registry=SQLTaskRegistry(),
                     authorizer=ApiKeyAuthorizer({"employee":Principal("E002","employee"),"hr":Principal("hr","hr")}))
    import asyncio
    import httpx
    async def request_checks():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/employees/E002", headers={"x-api-key":"employee"})).status_code == 200
            assert (await client.get("/employees/E003", headers={"x-api-key":"employee"})).status_code == 403
            result = await client.post("/agent/query/policy", json={"question":"remote work days"}, headers={"x-api-key":"employee"})
            assert result.json()["evidence_sufficient"]
            routed = await client.post("/agent/query", json={"question":"Check E002's leave balance"}, headers={"x-api-key":"employee"})
            assert routed.status_code == 200 and routed.json()["tools_used"] == ["get_leave_balance"]
            denied = await client.post("/agent/query", json={"question":"How many employees are in Engineering?"}, headers={"x-api-key":"employee"})
            assert denied.status_code == 403
    asyncio.run(request_checks())
