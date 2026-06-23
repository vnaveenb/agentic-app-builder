"""Smoke tests — no LLM calls, no network required, safe to run in CI."""

import pytest
from fastapi.testclient import TestClient

from src.dev_agent.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.mark.unit
def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200


@pytest.mark.unit
def test_health_response_schema(client: TestClient) -> None:
    data = client.get("/health").json()
    assert data["status"] in ("ok", "degraded")
    assert data["project"] == "project-7-ai-dev-agent"
    assert "provider" in data
    assert "runtimes" in data
    assert "python" in data["runtimes"]
    assert "node" in data["runtimes"]
    assert "static" in data["runtimes"]
    assert "database" in data
    assert "redis" in data


@pytest.mark.unit
def test_ui_returns_html(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
