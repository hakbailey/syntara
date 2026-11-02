"""Shared helper for entering the app lifespan from integration tests."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from syntara.api.main import app


@asynccontextmanager
async def enter_test_lifespan(test_db_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    """Run the app lifespan with test database properly configured.

    Args:
        test_db_engine: Test database engine with migrations applied

    """
    # Create test session factory from the test database engine
    test_session_factory = async_sessionmaker(test_db_engine, class_=AsyncSession, expire_on_commit=False)

    # Mock authz evaluator so the lifespan health check passes
    mock_evaluator = AsyncMock()
    mock_evaluator.health = AsyncMock(return_value=True)
    mock_evaluator.start = MagicMock()
    mock_evaluator.stop = AsyncMock()
    mock_evaluator.evaluate = MagicMock(return_value={"allow": True})

    # Patch all database connections to use the test database
    # This ensures _check_settings_catalog() and other startup code use the migrated test DB
    with (
        patch("syntara.core.database.session.engine", test_db_engine),
        patch("syntara.core.database.session.AsyncSessionLocal", test_session_factory),
        patch("syntara.api.main.engine", test_db_engine),
        patch("syntara.api.main.AsyncSessionLocal", test_session_factory),
        patch("syntara.audit.outbox.worker.AsyncSessionLocal", test_session_factory),
        patch("syntara.audit.outbox.worker.AuditWorkerAsyncSessionLocal", test_session_factory),
        patch("syntara.audit.outbox.session.AuditWorkerAsyncSessionLocal", test_session_factory),
        patch("syntara.api.main.RegoEvaluator", return_value=mock_evaluator),
    ):
        # Seed required data before app startup
        from syntara.core.seed import run_seeders

        await run_seeders(test_session_factory)

        # Run the app lifespan
        async with app.router.lifespan_context(app):
            yield
