"""The agent governance gate: MCP tool-call proxy and LLM completion broker.

Mounted at `/api/v1/agent-gate` (auto-discovered, see `core/router_discovery.py`).
Runs in the `syntara` API process, where the Rego evaluator is already
initialized (`api/main.py`'s lifespan) — the agent loop itself still runs in
`temporal-worker`, which has no evaluator of its own. Every tool call and LLM
completion request the agent makes crosses this boundary: authorized via
`authorize()`, audited via `AuditEventDispatcher`, with the target credential
resolved and injected here so the caller never holds the raw secret.

Callers authenticate as a service account bearer token, the same pattern
`workflows/webhook_router.py` uses for non-human callers.
"""

from typing import Annotated, Any
from uuid import UUID

import httpx
import structlog
from fastapi import Body, Depends, Header, Path, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from syntara.agent_orchestrator.audit.gate import (
    GateDecision,
    GateLLMCallEvent,
    GateToolCallEvent,
)
from syntara.agent_orchestrator.exceptions import GateAuthenticationRequiredError
from syntara.agent_orchestrator.gate.credentials import resolve_credential_field
from syntara.agent_orchestrator.gate.llm_broker import broker_llm_completion, stream_llm_completion
from syntara.agent_orchestrator.gate.mcp_proxy import call_mcp_tool
from syntara.agent_orchestrator.harness.schemas import HarnessInvocationRequest
from syntara.audit.dispatcher import AuditEventDispatcher
from syntara.audit.emitter import AuditActorContext
from syntara.auth.dependencies import (  # private helpers — same pattern as webhook_router.py
    _check_global_revocation,
    _get_token_service,
    _user_from_payload,
    bearer_scheme,
)
from syntara.auth.exceptions import InvalidTokenError
from syntara.authz.dependencies import get_authz_evaluator
from syntara.authz.engine import AuthzRequest, authorize
from syntara.authz.exceptions import AuthorizationDeniedError
from syntara.authz.models.project import Project
from syntara.core.config.base import get_settings
from syntara.core.database.session import get_db
from syntara.core.models import User
from syntara.core.models.principal import PrincipalType
from syntara.core.syntara_router import SyntaraRouter
from syntara.integrations.exceptions import IntegrationNotFoundError
from syntara.integrations.models.integration import Integration
from syntara.integrations.models.integration_configuration import (
    LLMProviderConfiguration,
    MCPServerConfiguration,
)

logger = structlog.stdlib.get_logger(__name__)

router = SyntaraRouter(prefix="/agent-gate", tags=["Agent Gate"])


# ============================================================================
# Caller authentication — same convention as workflows/webhook_router.py
# ============================================================================


async def get_gate_caller(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> tuple[User, UUID]:
    """Authenticate the gate caller as a service account.

    Returns:
        Tuple of (authenticated User, service account UUID).

    Raises:
        GateAuthenticationRequiredError: If no token or not a service account token.

    """
    if not credentials:
        raise GateAuthenticationRequiredError

    token_service = _get_token_service()
    try:
        payload = token_service.decode_token(credentials.credentials, token_type="access")  # noqa: S106
    except InvalidTokenError as e:
        logger.info("Gate auth failed: invalid token", error=str(e))
        raise GateAuthenticationRequiredError from e

    await _check_global_revocation(payload, token_type="access", db=db)  # noqa: S106

    if payload.token_type != "service_account":  # noqa: S105
        raise GateAuthenticationRequiredError

    user = _user_from_payload(payload)
    sa_id = UUID(payload.sub)
    return user, sa_id


def _actor_context(user: User, sa_id: UUID) -> AuditActorContext:
    return AuditActorContext(actor_id=sa_id, actor_username=user.username, actor_type=PrincipalType.SERVICE_ACCOUNT)


async def _resolve_gate_project(db: AsyncSession, resource_project: str) -> str:
    """Normalize a caller-supplied project UUID to the name used by Rego scopes."""
    if not resource_project:
        return resource_project
    try:
        project_id = UUID(resource_project)
    except ValueError:
        return resource_project
    project = await db.get(Project, project_id)
    return project.name if project else resource_project


# ============================================================================
# Request/response models
# ============================================================================


class ToolCallRequest(BaseModel):
    """A tool call proxied through the gate.

    `resource_type`/`action`/`resource_project` are caller-declared for this
    MVP — there is no `tool:execute` action or per-tool permission mapping
    yet (that is Prototype D's job). The gate's job here is the enforcement
    mechanics: call `authorize()`, audit the decision, inject the credential,
    forward or refuse.
    """

    arguments: dict[str, Any] = Field(default_factory=dict)
    credential_id: str | None = Field(default=None, description="Execution credential to inject, if any")
    resource_type: str = Field(description="authz resource_type to check, e.g. 'tool'")
    action: str = Field(description="authz action to check, e.g. 'read'")
    resource_project: str = Field(default="")
    session_id: str = Field(default="")
    invocation_id: UUID | None = None


class LLMCompletionRequest(BaseModel):
    """An LLM completion request brokered through the gate."""

    credential_id: str = Field(description="LLM credential to inject")
    request_body: dict[str, Any] = Field(description="OpenAI-compatible chat completion request body")
    session_id: str = Field(default="")
    invocation_id: UUID | None = None


# ============================================================================
# MCP tool-call proxy
# ============================================================================


@router.post("/mcp/{integration_id}/tools/{tool_name}/call")
async def proxy_mcp_tool_call(
    integration_id: Annotated[UUID, Path()],
    tool_name: Annotated[str, Path()],
    body: Annotated[ToolCallRequest, Body()],
    http_request: Request,
    caller: Annotated[tuple[User, UUID], Depends(get_gate_caller)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Any:  # noqa: ANN401
    """Authorize, audit, and (if allowed) forward one MCP tool call.

    The target integration's execution credential — if any — is resolved and
    injected here; the caller only ever supplies a `credential_id` reference.
    """
    user, sa_id = caller
    actor_context = _actor_context(user, sa_id)
    resource_project = await _resolve_gate_project(db, body.resource_project)

    evaluator = get_authz_evaluator(http_request)
    authz_result = await authorize(
        db,
        evaluator,
        AuthzRequest(
            user_id=sa_id,
            action=body.action,
            resource_type=body.resource_type,
            resource_id=tool_name,
            resource_project=resource_project,
        ),
    )

    if not authz_result.allowed:
        AuditEventDispatcher.dispatch(
            GateToolCallEvent(
                decision=GateDecision.DENIED,
                tool_name=tool_name,
                integration_id=integration_id,
                resource_type=body.resource_type,
                action=body.action,
                actor_context=actor_context,
                denied_by=authz_result.denied_by,
                session_id=body.session_id,
                invocation_id=body.invocation_id,
                resource_project=resource_project,
            )
        )
        msg = f"Not authorized to perform {body.action} on {body.resource_type} '{tool_name}'"
        raise AuthorizationDeniedError(msg)

    integration = await db.get(Integration, integration_id)
    if not integration or not isinstance(integration.configuration, MCPServerConfiguration):
        AuditEventDispatcher.dispatch(
            GateToolCallEvent(
                decision=GateDecision.ERROR,
                tool_name=tool_name,
                integration_id=integration_id,
                resource_type=body.resource_type,
                action=body.action,
                actor_context=actor_context,
                error_type="IntegrationNotFound",
                session_id=body.session_id,
                invocation_id=body.invocation_id,
                resource_project=resource_project,
            )
        )
        raise IntegrationNotFoundError(integration_id)

    bearer_token: str | None = None
    if body.credential_id:
        bearer_token = await resolve_credential_field(
            db, body.credential_id, field_name="bearer_token", label="execution credential"
        )

    config = integration.configuration
    try:
        result = await call_mcp_tool(
            base_url=config.base_url,
            tool_name=tool_name,
            arguments=body.arguments,
            bearer_token=bearer_token,
            integration_id=integration_id,
            insecure_skip_tls_verify=config.insecure_skip_tls_verify,
            ca_certificate=config.ca_certificate,
        )
    except Exception as e:
        AuditEventDispatcher.dispatch(
            GateToolCallEvent(
                decision=GateDecision.ERROR,
                tool_name=tool_name,
                integration_id=integration_id,
                resource_type=body.resource_type,
                action=body.action,
                actor_context=actor_context,
                error_type=type(e).__name__,
                session_id=body.session_id,
                invocation_id=body.invocation_id,
                resource_project=resource_project,
            )
        )
        raise

    AuditEventDispatcher.dispatch(
        GateToolCallEvent(
            decision=GateDecision.ALLOWED,
            tool_name=tool_name,
            integration_id=integration_id,
            resource_type=body.resource_type,
            action=body.action,
            actor_context=actor_context,
            session_id=body.session_id,
            invocation_id=body.invocation_id,
            resource_project=resource_project,
        )
    )
    return result


@router.post("/llm/{integration_id}/openai/v1/chat/completions", response_model=None)
async def broker_openai_compatible_call(
    integration_id: Annotated[UUID, Path()],
    body: Annotated[dict[str, Any], Body()],
    caller: Annotated[tuple[User, UUID], Depends(get_gate_caller)],
    db: Annotated[AsyncSession, Depends(get_db)],
    credential_id: Annotated[str | None, Header(alias="X-Syntara-Credential-ID")] = None,
    session_id: Annotated[str, Header(alias="X-Syntara-Session-ID")] = "",
    invocation_id: Annotated[UUID | None, Header(alias="X-Syntara-Invocation-ID")] = None,
) -> dict[str, Any] | StreamingResponse:
    """Expose the credential broker as an OpenAI-compatible endpoint."""
    if not credential_id:
        raise GateAuthenticationRequiredError
    user, sa_id = caller
    actor_context = _actor_context(user, sa_id)
    integration = await db.get(Integration, integration_id)
    if not integration or not isinstance(integration.configuration, LLMProviderConfiguration):
        raise IntegrationNotFoundError(integration_id)
    api_key = await resolve_credential_field(db, credential_id, field_name="llm_api_key", label="LLM credential")
    config = integration.configuration
    base_url = config.base_url or "https://openrouter.ai/api/v1"

    if body.get("stream"):

        async def chunks() -> Any:  # noqa: ANN401
            try:
                async for chunk in stream_llm_completion(
                    base_url=base_url,
                    api_key=api_key or "",
                    request_body=body,
                    insecure_skip_tls_verify=config.insecure_skip_tls_verify,
                ):
                    yield chunk
            except Exception as exc:
                AuditEventDispatcher.dispatch(
                    GateLLMCallEvent(
                        decision=GateDecision.ERROR,
                        integration_id=integration_id,
                        actor_context=actor_context,
                        error_type=type(exc).__name__,
                        session_id=session_id,
                        invocation_id=invocation_id,
                    )
                )
                raise
            AuditEventDispatcher.dispatch(
                GateLLMCallEvent(
                    decision=GateDecision.ALLOWED,
                    integration_id=integration_id,
                    actor_context=actor_context,
                    session_id=session_id,
                    invocation_id=invocation_id,
                )
            )

        return StreamingResponse(chunks(), media_type="text/event-stream")

    result = await broker_llm_completion(
        base_url=base_url,
        api_key=api_key or "",
        request_body=body,
        insecure_skip_tls_verify=config.insecure_skip_tls_verify,
    )
    AuditEventDispatcher.dispatch(
        GateLLMCallEvent(
            decision=GateDecision.ALLOWED,
            integration_id=integration_id,
            actor_context=actor_context,
            session_id=session_id,
            invocation_id=invocation_id,
        )
    )
    return result


@router.post("/harness/invocations")
async def relay_harness_invocation(
    body: Annotated[HarnessInvocationRequest, Body()],
    caller: Annotated[tuple[User, UUID], Depends(get_gate_caller)],
    authorization: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Relay worker traffic without joining the worker to the sandbox network."""
    _ = caller
    if not authorization:
        raise GateAuthenticationRequiredError
    harness_url = f"{str(get_settings().agent_harness_url).rstrip('/')}/invocations"
    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))
    try:
        upstream = await client.send(
            client.build_request(
                "POST",
                harness_url,
                headers={"Authorization": authorization},
                json=body.model_dump(mode="json"),
            ),
            stream=True,
        )
        upstream.raise_for_status()
    except Exception:
        await client.aclose()
        raise

    async def records() -> Any:  # noqa: ANN401
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(records(), media_type="application/x-ndjson")


# ============================================================================
# LLM completion broker
# ============================================================================


@router.post("/llm/{integration_id}/chat/completions")
async def broker_llm_call(
    integration_id: Annotated[UUID, Path()],
    body: Annotated[LLMCompletionRequest, Body()],
    caller: Annotated[tuple[User, UUID], Depends(get_gate_caller)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Inject the real LLM API key and forward a completion request.

    The caller only ever supplies a `credential_id` reference — the real key
    is resolved and injected here, never returned to or held by the caller.
    """
    user, sa_id = caller
    actor_context = _actor_context(user, sa_id)

    integration = await db.get(Integration, integration_id)
    if not integration or not isinstance(integration.configuration, LLMProviderConfiguration):
        AuditEventDispatcher.dispatch(
            GateLLMCallEvent(
                decision=GateDecision.ERROR,
                integration_id=integration_id,
                actor_context=actor_context,
                error_type="IntegrationNotFound",
                session_id=body.session_id,
                invocation_id=body.invocation_id,
            )
        )
        raise IntegrationNotFoundError(integration_id)

    api_key = await resolve_credential_field(db, body.credential_id, field_name="llm_api_key", label="LLM credential")
    config = integration.configuration
    base_url = config.base_url or "https://openrouter.ai/api/v1"

    try:
        result = await broker_llm_completion(
            base_url=base_url,
            api_key=api_key or "",
            request_body=body.request_body,
            insecure_skip_tls_verify=config.insecure_skip_tls_verify,
        )
    except Exception as e:
        AuditEventDispatcher.dispatch(
            GateLLMCallEvent(
                decision=GateDecision.ERROR,
                integration_id=integration_id,
                actor_context=actor_context,
                error_type=type(e).__name__,
                session_id=body.session_id,
                invocation_id=body.invocation_id,
            )
        )
        raise

    AuditEventDispatcher.dispatch(
        GateLLMCallEvent(
            decision=GateDecision.ALLOWED,
            integration_id=integration_id,
            actor_context=actor_context,
            session_id=body.session_id,
            invocation_id=body.invocation_id,
        )
    )
    return result
