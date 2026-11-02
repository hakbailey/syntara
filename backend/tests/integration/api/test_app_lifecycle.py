"""Integration tests for application lifecycle - audit system startup and shutdown."""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from syntara.audit.dispatcher import AuditEventDispatcher
from syntara.audit.outbox import session as outbox_session
from syntara.audit.outbox import worker as outbox_worker
from syntara.audit.outbox.session import AuditWorkerAsyncSessionLocal
from syntara.core.database.session import AsyncSessionLocal
from tests.integration.helpers.lifespan import enter_test_lifespan


@pytest.mark.asyncio
async def test_audit_dispatcher_registers_handlers_on_startup(test_db_engine: AsyncEngine) -> None:
    """Verify that audit handlers are discovered and registered during app startup (I0).

    This test ensures that the dispatcher bootstrap in main.py successfully:
    1. Discovers handlers from syntara.auth.audit
    2. Registers them with AuditEventDispatcher
    3. Results in a non-empty registry after startup
    """
    # Clear any pre-existing state
    AuditEventDispatcher._reset()
    assert len(AuditEventDispatcher._registry) == 0

    # Run the lifespan - startup should discover and register handlers
    async with enter_test_lifespan(test_db_engine):
        # Verify handlers were registered
        assert len(AuditEventDispatcher._registry) > 0, (
            "Expected audit handlers to be registered during startup, but registry is empty"
        )


@pytest.mark.asyncio
async def test_multiple_startup_shutdown_cycles_do_not_accumulate_handlers(test_db_engine: AsyncEngine) -> None:
    """Verify that multiple startup/shutdown cycles don't accumulate handlers (I1).

    This test ensures that handlers are properly cleaned up between cycles
    and don't leak across test sessions or app restarts.
    """
    # Clear any pre-existing state
    AuditEventDispatcher._reset()

    # First cycle
    async with enter_test_lifespan(test_db_engine):
        first_count = len(AuditEventDispatcher._registry)
        assert first_count > 0

    # Second cycle - should register the same number of handlers
    async with enter_test_lifespan(test_db_engine):
        second_count = len(AuditEventDispatcher._registry)
        assert second_count == first_count, (
            f"Expected {first_count} handlers in second cycle, but got {second_count}. "
            "Handlers may be accumulating across cycles."
        )


@pytest.mark.asyncio
async def test_lifespan_context_redirects_outbox_worker_factories(test_db_engine: AsyncEngine) -> None:
    """Verify enter_test_lifespan redirects every factory the outbox worker reads.

    Regression guard for the deterministic Integration CI failures seen in
    October 2026: this context historically omitted
    syntara.audit.outbox.worker.AsyncSessionLocal, so when this file ran
    before a worker's first session_app test, the lru_cache singleton behind
    get_outbox_worker() was created bound to the real database factories
    (localhost:5432, unreachable in CI) and every later audit write in that
    pytest-xdist worker failed after retries.
    """
    async with enter_test_lifespan(test_db_engine):
        # The dot access below needs Any-typed module aliases: worker.py and
        # session.py only import these factory names, so mypy
        # --no-implicit-reexport flags reading them as module attributes.
        worker_ns: Any = outbox_worker
        session_ns: Any = outbox_session
        assert worker_ns.AsyncSessionLocal is not AsyncSessionLocal, (
            "enter_test_lifespan must patch syntara.audit.outbox.worker.AsyncSessionLocal "
            "(the outbox write factory); otherwise get_outbox_worker() caches a singleton "
            "bound to the real database"
        )
        assert worker_ns.AuditWorkerAsyncSessionLocal is not AuditWorkerAsyncSessionLocal, (
            "enter_test_lifespan must patch syntara.audit.outbox.worker.AuditWorkerAsyncSessionLocal"
        )
        assert session_ns.AuditWorkerAsyncSessionLocal is not AuditWorkerAsyncSessionLocal, (
            "enter_test_lifespan must patch syntara.audit.outbox.session.AuditWorkerAsyncSessionLocal"
        )
