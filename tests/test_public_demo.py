from fastapi.testclient import TestClient

from sql_agent.api import create_app
from sql_agent.jobs import SqliteJobRepository
from sql_agent.sql_config import SQLTaskRegistry
from sql_agent.security import ApiKeyAuthorizer, Principal


def test_unscored_peopleops_evaluation_has_no_report(tmp_path):
    app = create_app(
        jobs=SqliteJobRepository(tmp_path / "jobs.sqlite"),
        registry=SQLTaskRegistry(), authorizer=ApiKeyAuthorizer({"test-only": Principal("test", "viewer")}),
    )
    with TestClient(app) as client:
        response = client.get("/benchmark", follow_redirects=False)
    assert response.status_code == 404


def test_peopleops_dashboard_does_not_link_unscored_benchmark(tmp_path):
    app = create_app(
        jobs=SqliteJobRepository(tmp_path / "jobs.sqlite"),
        registry=SQLTaskRegistry(), authorizer=ApiKeyAuthorizer({"test-only": Principal("test", "viewer")}),
    )
    with TestClient(app) as client:
        assert 'href="/benchmark"' not in client.get("/").text
