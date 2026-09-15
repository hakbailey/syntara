"""Tests for deferred invocation actor-context resolution."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from syntara.agent_orchestrator.executor.invocation_executor import InvocationExecutor
from syntara.agent_orchestrator.models import Invocation
from syntara.core.models import User
from syntara.core.models.principal import PrincipalType
from syntara.service_accounts.models.service_account import ServiceAccount


def _make_executor(session: MagicMock) -> InvocationExecutor:
    """Build an executor with an injected session context."""

    @asynccontextmanager
    async def session_context() -> AsyncGenerator[MagicMock, None]:
        yield session

    executor = InvocationExecutor.__new__(InvocationExecutor)
    executor.get_async_session_context = session_context
    executor.session_factory = session_context  # type: ignore[assignment]
    return executor


@pytest.mark.asyncio
async def test_service_account_creator_is_preserved_for_deferred_execution() -> None:
    principal_id = uuid4()
    invocation = MagicMock(spec=Invocation, created_by=principal_id)
    service_account = MagicMock(spec=ServiceAccount)
    service_account.id = principal_id
    service_account.name = "incident-triage-trigger"
    session = MagicMock()

    async def get(model: type[object], identifier: object) -> object | None:
        if model is User:
            return None
        if model is ServiceAccount and identifier == principal_id:
            return service_account
        return None

    session.get = AsyncMock(side_effect=get)
    actor = await _make_executor(session)._get_actor_context_for_invocation(invocation)

    assert actor.actor_id == principal_id
    assert actor.actor_username == "incident-triage-trigger"
    assert actor.actor_type == PrincipalType.SERVICE_ACCOUNT


@pytest.mark.asyncio
async def test_runtime_service_account_overrides_workflow_creator() -> None:
    creator_id = uuid4()
    runtime_service_account_id = uuid4()
    invocation = MagicMock(
        spec=Invocation,
        created_by=creator_id,
        context_data={"runtime_service_account_id": str(runtime_service_account_id)},
    )
    service_account = MagicMock(spec=ServiceAccount)
    service_account.id = runtime_service_account_id
    service_account.name = "incident-triage-trigger"
    session = MagicMock()

    async def get(model: type[object], identifier: object) -> object | None:
        if model is ServiceAccount and identifier == runtime_service_account_id:
            return service_account
        return None

    session.get = AsyncMock(side_effect=get)
    actor = await _make_executor(session)._get_actor_context_for_invocation(invocation)

    assert actor.actor_id == runtime_service_account_id
    assert actor.actor_username == "incident-triage-trigger"
    assert actor.actor_type == PrincipalType.SERVICE_ACCOUNT
    session.get.assert_awaited_once_with(ServiceAccount, runtime_service_account_id)
