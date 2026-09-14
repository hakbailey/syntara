"""Unit tests for the isolated harness HTTP boundary."""

from fastapi.testclient import TestClient

from syntara.agent_orchestrator.harness.app import app


def test_healthz_is_public_and_dependency_free() -> None:
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_invocation_requires_scoped_bearer() -> None:
    response = TestClient(app).post("/invocations", json={})

    assert response.status_code == 422


def test_valid_invocation_without_bearer_is_rejected() -> None:
    response = TestClient(app).post(
        "/invocations",
        json={
            "prompt": "prompt",
            "original_prompt": "prompt",
            "system_prompt": "system",
            "session_id": "session",
            "invocation_id": "00000000-0000-0000-0000-000000000001",
            "model_name": "test/model",
            "llm_integration_id": "00000000-0000-0000-0000-000000000002",
            "llm_credential_id": "00000000-0000-0000-0000-000000000003",
        },
    )

    assert response.status_code == 401
