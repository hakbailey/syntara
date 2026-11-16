"""Shared helper for entering the app lifespan from integration tests.

The lifecycle suite runs in its own pytest invocation (``make test-lifespan``)
rather than as part of ``tests/integration``. That keeps the session-scoped
``session_app`` fixture out of the process, so no second lifespan has to share
process-global workers with a live app. The helper still restores the app
singleton's own state (``app.state``, routes, interceptors) and the logging
configuration, because the tests in that file boot the lifespan several times
in one process. A live worker is treated as a caller error and skipped.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

import syntara.audit.lifecycle as audit_lifecycle
from syntara.api.main import app
from syntara.audit.logging import AUDIT_LOGGER_NAME
from syntara.audit.outbox.worker import get_outbox_worker
from syntara.core.logging import lifecycle as logging_lifecycle
from syntara.core.websocket.interceptor import get_registry
from syntara.core.websocket.manager import get_connection_lifecycle_manager
from syntara.files.workers.file_cleanup import get_multipart_cleanup_worker
from syntara.settings.cache import settings_cache
from syntara.settings.cache.settings_cache import SettingsCache
from syntara.workflow_runtime.workers.schedule_reconciliation import get_schedule_reconciliation_worker

_MISSING = object()

# app.state attributes the lifespan sets at startup but never clears at shutdown.
# Restoring them keeps later tests in the same process from reading this test's
# instances.
LEAKED_STATE_ATTRS = ("authz_evaluator", "resource_actions", "rate_limit_redis", "rate_limit_token_bucket")

# apply_runtime_log_level() sets the level on these loggers as well as root, so
# every lifespan entry mutates them and the restore below must put them back.
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

# Single source of truth for the database bindings enter_test_lifespan and the
# session_app fixture must patch. Both previously hand-maintained identical lists;
# a target added to one but not the other silently reintroduced the PR #921 leak
# class, so they share these tuples through patched_test_db below.
ENGINE_TARGETS: tuple[str, ...] = (
    "syntara.core.database.session.engine",
    "syntara.api.main.engine",
)
# Modules that bind AsyncSessionLocal at import
# (from syntara.core.database.session import AsyncSessionLocal) never see a
# patch of the session module attribute, so each needs its own target. Without
# these patches their lru_cache getters would cache workers bound to the real
# database factories for the rest of this process, and a later startup would
# reuse them against the wrong database. The per-call cleanup workers are not
# cached, but they read the module attribute every time they run, so they need
# the same patch to hit the test database. test_app_lifecycle.py pins the
# targets against the actual import-time bindings
# (test_db_factory_targets_match_import_time_bindings).
SESSION_FACTORY_TARGETS: tuple[str, ...] = (
    "syntara.core.database.session.AsyncSessionLocal",
    "syntara.api.main.AsyncSessionLocal",
    "syntara.audit.outbox.worker.AsyncSessionLocal",
    "syntara.audit.outbox.worker.AuditWorkerAsyncSessionLocal",
    "syntara.audit.outbox.session.AuditWorkerAsyncSessionLocal",
    "syntara.files.workers.file_cleanup.AsyncSessionLocal",
    "syntara.workflow_runtime.workers.schedule_reconciliation.AsyncSessionLocal",
    "syntara.metrics.completion_poller.AsyncSessionLocal",
    "syntara.metrics.cleanup.AsyncSessionLocal",
    "syntara.auth.session.cleanup.AsyncSessionLocal",
)


@contextlib.contextmanager
def patched_test_db(engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]) -> Iterator[None]:
    """Patch every engine and session-factory binding to the test database.

    Shared by enter_test_lifespan and the session_app fixture so the two
    call sites cannot drift apart: a factory or engine binding missed here
    means startup code (or a worker singleton cached during startup) runs
    against the real database for the rest of this process.
    """
    with contextlib.ExitStack() as stack:
        for target in ENGINE_TARGETS:
            stack.enter_context(patch(target, engine))
        for target in SESSION_FACTORY_TARGETS:
            stack.enter_context(patch(target, session_factory))
        yield


def task_live_on_running_loop(task: asyncio.Task[None] | None) -> bool:
    """Return whether the task is live and bound to the loop this helper runs on."""
    return task is not None and not task.done() and task.get_loop() is asyncio.get_running_loop()


def _singleton_worker_running() -> bool:
    """Return whether any process-wide worker is live.

    The lru_cache getters are inspected only when already populated, so this
    does not create a worker bound to the real database.
    """
    for getter in (get_outbox_worker, get_multipart_cleanup_worker, get_schedule_reconciliation_worker):
        if getter.cache_info().currsize == 0:
            continue
        task = getter()._task
        if task is not None and not task.done():
            return True
    monitoring_task = get_connection_lifecycle_manager()._monitoring_task
    return monitoring_task is not None and not monitoring_task.done()


def skip_if_singleton_workers_running() -> None:
    """Skip when a process-wide worker is already live.

    The lifecycle suite is meant to run without a live ``session_app``; if a
    worker survived an earlier test, the helper cannot restart it safely and
    would leave it stopped for the rest of the process.
    """
    if _singleton_worker_running():
        pytest.skip("a process-wide worker is already running; run the lifecycle suite in isolation")


def _restore_leaked_state_attrs(prior_state: dict[str, Any]) -> None:
    """Undo the app.state attributes the lifespan set at startup but never clears."""
    for name, prior in prior_state.items():
        if prior is _MISSING:
            if hasattr(app.state, name):
                delattr(app.state, name)
        else:
            setattr(app.state, name, prior)


async def stop_leftover_audit_subsystems() -> None:
    """Stop audit subsystems a failed startup left RUNNING.

    A clean shutdown stops them itself; this is a no-op then.
    """
    if audit_lifecycle._state is audit_lifecycle.AuditLifecycleState.RUNNING:
        with contextlib.suppress(Exception):
            await audit_lifecycle.stop_audit_subsystems()


async def stop_leftover_workers() -> None:
    """Stop singleton workers a failed startup left running.

    FastAPI's lifespan_context skips shutdown when startup raises, so a
    mid-startup failure after the workers start leaves the process-wide
    singletons running. A clean shutdown has already stopped them, so this is
    a no-op then. The per-call workers (metrics pollers, session cleanup,
    periodic collector) cannot be reclaimed this way: their getters return
    fresh instances nobody holds a reference to.
    """
    ws_manager = get_connection_lifecycle_manager()
    if task_live_on_running_loop(ws_manager._monitoring_task):
        with contextlib.suppress(Exception):
            ws_manager.stop_monitoring()
    for getter in (get_multipart_cleanup_worker, get_schedule_reconciliation_worker):
        if getter.cache_info().currsize == 0:
            continue
        worker = getter()
        if task_live_on_running_loop(worker._task):
            with contextlib.suppress(Exception):
                await worker.stop()


def clear_singleton_caches() -> None:
    """Drop the singleton workers cached during this entry.

    The cached workers are bound to this test's patched session factory, so
    the next entry must rebuild them against the restored module attributes
    rather than reuse a worker holding a disposed engine.
    """
    get_outbox_worker.cache_clear()
    get_multipart_cleanup_worker.cache_clear()
    get_schedule_reconciliation_worker.cache_clear()


async def stop_replaced_settings_watcher(prior_runtime_settings: SettingsCache | None) -> None:
    """Stop a settings watcher that nested startup left running.

    Nested shutdown stops the nested cache's watcher in the normal path, but a
    startup failure after start_watching() skips FastAPI's shutdown entirely
    and leaves it polling. Its change callback can still mutate root and
    uvicorn log levels, so stop it before the caller restores the prior cache
    and log state.
    """
    current = settings_cache._runtime_settings
    if current is prior_runtime_settings or current is None:
        return
    with contextlib.suppress(Exception):
        await current.stop_watching()


async def clear_singleton_caches_stopping_live_workers() -> None:
    """Stop workers still live on this loop, then drop every singleton cache.

    The session_app fixture needs every worker rebuilt inside its own factory
    patch set, but dropping a cached worker whose task is still running would
    strand the task for the rest of the pytest-xdist worker.
    """
    await stop_leftover_workers()
    if get_outbox_worker.cache_info().currsize > 0 and task_live_on_running_loop(get_outbox_worker()._task):
        with contextlib.suppress(Exception):
            await audit_lifecycle.stop_audit_outbox_worker()
    clear_singleton_caches()


@asynccontextmanager
async def enter_test_lifespan(test_db_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    """Run the app lifespan with the test database properly configured.

    Every entry expects the process-wide singleton workers to be stopped and
    restarts nothing: the lifecycle suite runs in its own process, so a worker
    should never be live on entry, and the guard below skips if one is rather
    than stranding it for the rest of the process.

    Args:
        test_db_engine: Test database engine with migrations applied

    """
    skip_if_singleton_workers_running()

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
        patched_test_db(test_db_engine, test_session_factory),
        patch("syntara.api.main.RegoEvaluator", return_value=mock_evaluator),
    ):
        # Seed required data before app startup
        from syntara.core.seed import run_seeders

        await run_seeders(test_session_factory)

        # The lifespan sets each LEAKED_STATE_ATTRS entry at startup but never
        # clears it at shutdown; without restoring the prior values the test's
        # instances leak to later tests in this process.
        prior_state: dict[str, Any] = {name: getattr(app.state, name, _MISSING) for name in LEAKED_STATE_ATTRS}

        # The lifespan also mutates process-global state beyond app.state: it
        # replaces the process-wide runtime settings cache, appends a duplicate
        # route set to the shared app, and removes every root logger handler.
        # Capture them here and restore them on exit so the tests in this file
        # can boot the lifespan repeatedly in one process.
        prior_runtime_settings = settings_cache._runtime_settings
        prior_logging_state = logging_lifecycle._logging_state
        prior_routes = list(app.router.routes)
        # build_websocket_router() appends a ValidationInterceptor to the
        # process-global registry on every startup with no dedup guard.
        prior_interceptors = list(get_registry()._interceptors)
        root_logger = logging.getLogger()
        prior_root_handlers = list(root_logger.handlers)
        prior_root_level = root_logger.level
        prior_uvicorn_levels = {name: logging.getLogger(name).level for name in UVICORN_LOGGERS}
        audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
        prior_audit_log_handlers = list(audit_logger.handlers)
        prior_audit_log_level = audit_logger.level
        # configure_audit_logging() forces propagate=False; leaving it set
        # changes how later audit records reach the root logger and pytest
        # logging capture.
        prior_audit_log_propagate = audit_logger.propagate

        # Run the app lifespan
        try:
            async with app.router.lifespan_context(app):
                yield
        finally:
            _restore_leaked_state_attrs(prior_state)
            await stop_replaced_settings_watcher(prior_runtime_settings)
            # Restore the process-wide runtime settings cache: nested startup replaced
            # it and nested shutdown only stopped the nested cache's watcher, which
            # would leave later tests reading settings through an orphaned cache.
            settings_cache._runtime_settings = prior_runtime_settings
            # Undo the duplicate route registration: router discovery and the manual
            # WebSocket router include have no dedup guard, so every entry appends a
            # full route set to the singleton app for the rest of this process.
            app.router.routes[:] = prior_routes
            get_registry()._interceptors[:] = prior_interceptors
            await stop_leftover_audit_subsystems()
            await stop_leftover_workers()
            clear_singleton_caches()
            # Restore the logging subsystems directly rather than restarting them via
            # start_loggers(): it installs fresh stdout/OTLP handlers (duplicating the
            # prior ones) and cannot restore handlers the lifespan never owned.
            root_logger.handlers[:] = prior_root_handlers
            root_logger.setLevel(prior_root_level)
            for name, level in prior_uvicorn_levels.items():
                logging.getLogger(name).setLevel(level)
            audit_logger.handlers[:] = prior_audit_log_handlers
            audit_logger.setLevel(prior_audit_log_level)
            audit_logger.propagate = prior_audit_log_propagate
            logging_lifecycle._logging_state = prior_logging_state
