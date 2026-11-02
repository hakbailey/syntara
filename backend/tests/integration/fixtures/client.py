"""FastAPI test client fixtures for integration tests."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import structlog
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from syntara.audit.outbox.worker import get_outbox_worker
from syntara.auth.dependencies import get_current_user
from syntara.core.database.session import get_db
from syntara.files.workers.file_cleanup import get_multipart_cleanup_worker
from syntara.workflows.services.execution_streaming_service import ExecutionStreamingService
from syntara.workflows.workers.schedule_reconciliation import get_schedule_reconciliation_worker

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

    from fastapi import FastAPI
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from syntara.core.models import User

logger = structlog.stdlib.get_logger(__name__)


@asynccontextmanager
async def _scoped_overrides(app: FastAPI) -> AsyncGenerator[None, None]:
    """Save and restore dependency_overrides around a test fixture."""
    saved = dict(app.dependency_overrides)
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


@pytest_asyncio.fixture(scope="session")
async def session_app(
    worker_id: str,
    test_db_engine: AsyncEngine,
    test_cache: None,
    test_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[FastAPI, None]:
    """Create a session-scoped app with routers discovered once per worker."""
    # Imported lazily so registering this plugin module does not import
    # syntara.api.main; test modules and fixtures that need the app import it
    # themselves.
    from syntara.api.main import app
    from tests.integration.helpers.lifespan import patched_test_db

    mock_evaluator = AsyncMock()
    mock_evaluator.health = AsyncMock(return_value=True)
    mock_evaluator.start = MagicMock()
    mock_evaluator.stop = AsyncMock()
    mock_evaluator.evaluate = AsyncMock(return_value={"allow": True})

    # A test that entered the app lifespan earlier may have cached workers
    # bound to another test's database factories (the outbox worker, and the
    # schedule-reconciliation and multipart-cleanup singletons whose modules
    # bind AsyncSessionLocal at import). Drop them so the lifespan below
    # constructs each worker inside this fixture's patch set; the two
    # singleton workers are also patched at their own module attributes below
    # because patching syntara.core.database.session.AsyncSessionLocal never
    # reaches their import-time bindings.
    get_outbox_worker.cache_clear()
    get_multipart_cleanup_worker.cache_clear()
    get_schedule_reconciliation_worker.cache_clear()

    with (
        # Same shared patch set as enter_test_lifespan: a factory or engine
        # binding missed here means the session app (or a worker singleton it
        # caches) runs against the real database for the rest of the worker.
        patched_test_db(test_db_engine, test_session_factory),
        patch("syntara.api.main.RegoEvaluator", return_value=mock_evaluator),
    ):
        from syntara.core.seed import run_seeders

        await run_seeders(test_session_factory)

        async with app.router.lifespan_context(app):
            # pytest-xdist runs each worker in a separate subprocess.
            # pytest's log_cli/log_file options only capture logs from the
            # controller process, not from workers, so application logs
            # (structlog routed through stdlib logging) are silently lost.
            # Additionally, this handler must be attached *after* the app
            # lifespan starts because configure_app_logging() clears all
            # root logger handlers during startup.
            logs_dir = Path("integration-test-logs")
            logs_dir.mkdir(exist_ok=True)
            xdist_id = os.environ.get("PYTEST_XDIST_WORKER", "main")
            fh = logging.FileHandler(logs_dir / f"{xdist_id}.log", mode="w")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
            root = logging.getLogger()
            root.addHandler(fh)
            root.setLevel(logging.DEBUG)

            logger.info("Session app initialized for worker '%s'", worker_id)
            yield app

            root.removeHandler(fh)
            fh.close()


@pytest_asyncio.fixture
async def base_client(test_db_session: AsyncSession, session_app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    """Create a base test client with database session override (no authentication)."""
    async with _scoped_overrides(session_app):

        async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
            yield test_db_session

        session_app.dependency_overrides[get_db] = override_get_db

        async with AsyncClient(
            transport=ASGITransport(app=session_app),
            base_url="http://test",
        ) as client:
            yield client


@pytest.fixture
def _override_temporal(
    session_app: FastAPI,
    temporal_env: WorkflowEnvironment,
    temporal_worker: Worker,
) -> None:
    """Add Temporal execution service to dependency overrides."""
    from syntara.workflow_runtime.engines.temporal.services.temporal_execution_service import TemporalExecutionService
    from syntara.workflows.executions_router import get_temporal_execution_service

    _svc = TemporalExecutionService(temporal_env.client, "test-workflow-queue", "test-workflow-queue")
    session_app.dependency_overrides[get_temporal_execution_service] = lambda: _svc


@pytest_asyncio.fixture
async def base_client_with_mocked_llm(
    base_client: AsyncClient, mock_openrouter_llm: MagicMock, _override_temporal: None
) -> AsyncClient:
    """Base test client with mocked LLM and Temporal support."""
    return base_client


@pytest_asyncio.fixture
async def auth_client(base_client: AsyncClient, test_user: User) -> AsyncClient:
    """Create an authenticated test client with test_user."""
    from syntara.api.main import app

    async def override_get_current_user() -> User:
        return test_user

    app.dependency_overrides[get_current_user] = override_get_current_user
    return base_client


@pytest_asyncio.fixture
async def auth_client_with_mocked_llm(base_client_with_mocked_llm: AsyncClient, test_user: User) -> AsyncClient:
    """Create an authenticated test client with mocked OpenRouter LLM."""
    from syntara.api.main import app

    async def override_get_current_user() -> User:
        return test_user

    app.dependency_overrides[get_current_user] = override_get_current_user
    return base_client_with_mocked_llm


@pytest.fixture
def sync_test_client(
    session_app: FastAPI,
    test_db_session: AsyncSession,
    test_db_engine: AsyncEngine,
) -> Generator[TestClient, None, None]:
    """Create a synchronous test client with DB and streaming overrides."""
    from syntara.api.main import app

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield test_db_session

    previous_get_db = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db

    session_factory = async_sessionmaker(
        test_db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    previous_streaming_service = getattr(app.state, "execution_streaming_service", None)
    app.state.execution_streaming_service = ExecutionStreamingService(session_factory=session_factory)

    mock_evaluator = AsyncMock()
    mock_evaluator.health = AsyncMock(return_value=True)
    mock_evaluator.start = MagicMock()
    mock_evaluator.stop = AsyncMock()
    mock_evaluator.evaluate = AsyncMock(return_value={"allow": True})

    try:
        from tests.integration.helpers.lifespan import patched_test_db

        with (
            patched_test_db(test_db_engine, session_factory),
            patch("syntara.api.main.RegoEvaluator", return_value=mock_evaluator),
        ):
            client = TestClient(app)
            try:
                yield client
            finally:
                client.close()
    finally:
        if previous_get_db is not None:
            app.dependency_overrides[get_db] = previous_get_db
        else:
            app.dependency_overrides.pop(get_db, None)

        if previous_streaming_service is not None:
            app.state.execution_streaming_service = previous_streaming_service
        elif hasattr(app.state, "execution_streaming_service"):
            delattr(app.state, "execution_streaming_service")
