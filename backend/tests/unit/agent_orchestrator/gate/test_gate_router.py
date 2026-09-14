"""Unit tests for the agent governance gate router.

Covers the `get_gate_caller` auth dependency (mirrors
`workflows/webhook_router.py`'s `get_webhook_caller` tests) and the two route
handlers with mocked `authorize`/DB/provider calls.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.security import HTTPAuthorizationCredentials

from syntara.agent_orchestrator.audit.gate import GateDecision
from syntara.agent_orchestrator.exceptions import GateAuthenticationRequiredError
from syntara.agent_orchestrator.gate_router import (
    LLMCompletionRequest,
    ToolCallRequest,
    broker_llm_call,
    get_gate_caller,
    proxy_mcp_tool_call,
)
from syntara.auth.exceptions import InvalidTokenError
from syntara.auth.services.token_service import TokenPayload
from syntara.authz.exceptions import AuthorizationDeniedError
from syntara.integrations.exceptions import IntegrationNotFoundError
from syntara.integrations.models.integration_configuration import (
    LLMProviderConfiguration,
    LLMProviderHint,
    MCPServerConfiguration,
)


def _make_sa_payload(sa_id: str | None = None) -> TokenPayload:
    now = datetime.now(UTC)
    return TokenPayload(
        sub=sa_id or str(uuid4()),
        iss="https://test",
        aud="orchestrator-api",
        iat=now,
        exp=now,
        token_type="service_account",  # noqa: S106
        preferred_username="test-sa",
    )


def _make_user_payload() -> TokenPayload:
    now = datetime.now(UTC)
    return TokenPayload(
        sub=str(uuid4()),
        iss="https://test",
        aud="orchestrator-api",
        iat=now,
        exp=now,
        token_type="access",  # noqa: S106
        preferred_username="testuser",
    )


class TestGetGateCaller:
    """Tests for the get_gate_caller auth dependency."""

    async def test_no_credentials_raises(self) -> None:
        with pytest.raises(GateAuthenticationRequiredError):
            await get_gate_caller(credentials=None, db=MagicMock())

    async def test_invalid_token_raises(self) -> None:
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="bad-token")
        with patch("syntara.agent_orchestrator.gate_router._get_token_service") as mock_ts:
            mock_ts.return_value.decode_token.side_effect = InvalidTokenError
            with pytest.raises(GateAuthenticationRequiredError):
                await get_gate_caller(credentials=credentials, db=MagicMock())

    async def test_non_service_account_token_raises(self) -> None:
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="user-token")
        user_payload = _make_user_payload()
        with (
            patch("syntara.agent_orchestrator.gate_router._get_token_service") as mock_ts,
            patch("syntara.agent_orchestrator.gate_router._check_global_revocation", new=AsyncMock()),
        ):
            mock_ts.return_value.decode_token.return_value = user_payload
            with pytest.raises(GateAuthenticationRequiredError):
                await get_gate_caller(credentials=credentials, db=MagicMock())

    async def test_valid_service_account_returns_caller(self) -> None:
        sa_id = str(uuid4())
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="sa-token")
        sa_payload = _make_sa_payload(sa_id)
        with (
            patch("syntara.agent_orchestrator.gate_router._get_token_service") as mock_ts,
            patch("syntara.agent_orchestrator.gate_router._check_global_revocation", new=AsyncMock()),
        ):
            mock_ts.return_value.decode_token.return_value = sa_payload
            user, returned_sa_id = await get_gate_caller(credentials=credentials, db=MagicMock())

        assert str(returned_sa_id) == sa_id
        assert user.username == "test-sa"


def _mock_mcp_integration() -> MagicMock:
    integration = MagicMock()
    integration.configuration = MCPServerConfiguration(base_url="https://mcp.example.com")
    return integration


def _mock_llm_integration() -> MagicMock:
    integration = MagicMock()
    integration.configuration = LLMProviderConfiguration(
        provider_hint=LLMProviderHint.OPENAI, base_url="https://llm.example.com"
    )
    return integration


class TestProxyMcpToolCall:
    """Tests for the MCP tool-call proxy route handler."""

    async def test_denied_blocks_and_audits_without_forwarding(self) -> None:
        caller = (MagicMock(username="test-sa"), uuid4())
        body = ToolCallRequest(resource_type="credential", action="read")
        db = MagicMock()

        with (
            patch("syntara.agent_orchestrator.gate_router.get_authz_evaluator"),
            patch("syntara.agent_orchestrator.gate_router.authorize", new=AsyncMock()) as mock_authorize,
            patch("syntara.agent_orchestrator.gate_router.call_mcp_tool", new=AsyncMock()) as mock_call_tool,
            patch("syntara.agent_orchestrator.gate_router.AuditEventDispatcher") as mock_dispatcher,
        ):
            mock_authorize.return_value = MagicMock(allowed=False, denied_by="no matching policy")

            with pytest.raises(AuthorizationDeniedError):
                await proxy_mcp_tool_call(
                    integration_id=uuid4(),
                    tool_name="get_incident_logs",
                    body=body,
                    http_request=MagicMock(),
                    caller=caller,
                    db=db,
                )

        mock_call_tool.assert_not_awaited()
        dispatched_event = mock_dispatcher.dispatch.call_args[0][0]
        assert dispatched_event.decision == GateDecision.DENIED
        assert dispatched_event.denied_by == "no matching policy"

    async def test_allowed_injects_credential_and_forwards(self) -> None:
        caller = (MagicMock(username="test-sa"), uuid4())
        integration_id = uuid4()
        body = ToolCallRequest(resource_type="tool", action="read", credential_id=str(uuid4()))
        db = MagicMock()
        db.get = AsyncMock(return_value=_mock_mcp_integration())

        with (
            patch("syntara.agent_orchestrator.gate_router.get_authz_evaluator"),
            patch("syntara.agent_orchestrator.gate_router.authorize", new=AsyncMock()) as mock_authorize,
            patch(
                "syntara.agent_orchestrator.gate_router.resolve_credential_field", new=AsyncMock()
            ) as mock_resolve_cred,
            patch("syntara.agent_orchestrator.gate_router.call_mcp_tool", new=AsyncMock()) as mock_call_tool,
            patch("syntara.agent_orchestrator.gate_router.AuditEventDispatcher") as mock_dispatcher,
        ):
            mock_authorize.return_value = MagicMock(allowed=True)
            mock_resolve_cred.return_value = "resolved-bearer-token"
            mock_call_tool.return_value = {"logs": []}

            result = await proxy_mcp_tool_call(
                integration_id=integration_id,
                tool_name="get_incident_logs",
                body=body,
                http_request=MagicMock(),
                caller=caller,
                db=db,
            )

        assert result == {"logs": []}
        mock_call_tool.assert_awaited_once()
        assert mock_call_tool.call_args.kwargs["bearer_token"] == "resolved-bearer-token"  # noqa: S105
        dispatched_event = mock_dispatcher.dispatch.call_args[0][0]
        assert dispatched_event.decision == GateDecision.ALLOWED

    async def test_project_uuid_is_normalized_for_project_scoped_policy(self) -> None:
        caller = (MagicMock(username="test-sa"), uuid4())
        integration_id = uuid4()
        project_id = uuid4()
        body = ToolCallRequest(
            resource_type="tool",
            action="read",
            resource_project=str(project_id),
        )
        db = MagicMock()
        db.get = AsyncMock(
            side_effect=[
                SimpleNamespace(name="incident-triage-prototype"),
                _mock_mcp_integration(),
            ]
        )

        with (
            patch("syntara.agent_orchestrator.gate_router.get_authz_evaluator"),
            patch("syntara.agent_orchestrator.gate_router.authorize", new=AsyncMock()) as mock_authorize,
            patch("syntara.agent_orchestrator.gate_router.call_mcp_tool", new=AsyncMock(return_value={"logs": []})),
            patch("syntara.agent_orchestrator.gate_router.AuditEventDispatcher"),
        ):
            mock_authorize.return_value = MagicMock(allowed=True)
            await proxy_mcp_tool_call(
                integration_id=integration_id,
                tool_name="get_incident_logs",
                body=body,
                http_request=MagicMock(),
                caller=caller,
                db=db,
            )

        request = mock_authorize.call_args.args[2]
        assert request.resource_project == "incident-triage-prototype"

    async def test_unknown_integration_raises_not_found(self) -> None:
        caller = (MagicMock(username="test-sa"), uuid4())
        body = ToolCallRequest(resource_type="tool", action="read")
        db = MagicMock()
        db.get = AsyncMock(return_value=None)

        with (
            patch("syntara.agent_orchestrator.gate_router.get_authz_evaluator"),
            patch("syntara.agent_orchestrator.gate_router.authorize", new=AsyncMock()) as mock_authorize,
            patch("syntara.agent_orchestrator.gate_router.AuditEventDispatcher"),
        ):
            mock_authorize.return_value = MagicMock(allowed=True)
            with pytest.raises(IntegrationNotFoundError):
                await proxy_mcp_tool_call(
                    integration_id=uuid4(),
                    tool_name="get_incident_logs",
                    body=body,
                    http_request=MagicMock(),
                    caller=caller,
                    db=db,
                )


class TestBrokerLlmCall:
    """Tests for the LLM completion broker route handler."""

    async def test_injects_key_and_forwards(self) -> None:
        caller = (MagicMock(username="test-sa"), uuid4())
        integration_id = uuid4()
        body = LLMCompletionRequest(credential_id=str(uuid4()), request_body={"model": "gpt-4"})
        db = MagicMock()
        db.get = AsyncMock(return_value=_mock_llm_integration())

        with (
            patch(
                "syntara.agent_orchestrator.gate_router.resolve_credential_field", new=AsyncMock()
            ) as mock_resolve_cred,
            patch("syntara.agent_orchestrator.gate_router.broker_llm_completion", new=AsyncMock()) as mock_broker,
            patch("syntara.agent_orchestrator.gate_router.AuditEventDispatcher") as mock_dispatcher,
        ):
            mock_resolve_cred.return_value = "sk-real-key"
            mock_broker.return_value = {"choices": []}

            result = await broker_llm_call(integration_id=integration_id, body=body, caller=caller, db=db)

        assert result == {"choices": []}
        assert mock_broker.call_args.kwargs["api_key"] == "sk-real-key"
        dispatched_event = mock_dispatcher.dispatch.call_args[0][0]
        assert dispatched_event.decision == GateDecision.ALLOWED

    async def test_unknown_integration_raises_not_found(self) -> None:
        caller = (MagicMock(username="test-sa"), uuid4())
        body = LLMCompletionRequest(credential_id=str(uuid4()), request_body={})
        db = MagicMock()
        db.get = AsyncMock(return_value=None)

        with patch("syntara.agent_orchestrator.gate_router.AuditEventDispatcher"):
            with pytest.raises(IntegrationNotFoundError):
                await broker_llm_call(integration_id=uuid4(), body=body, caller=caller, db=db)
