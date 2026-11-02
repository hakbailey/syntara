"""Shared helper for entering the app lifespan from integration tests."""

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
from syntara.workflows.workers.schedule_reconciliation import get_schedule_reconciliation_worker

_MISSING = object()

# app.state attributes the lifespan sets at startup but never clears at shutdown.
# Entering the lifespan from a test must leave each one as it found it, or the
# test's instances leak to every later test in the same pytest-xdist worker.
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
# database factories for the rest of this worker, and a later session_app
# would reuse them against the wrong database. test_app_lifecycle.py pins the
# targets against the actual import-time bindings
# (test_db_factory_targets_match_import_time_bindings).
SESSION_FACTORY_TARGETS: tuple[str, ...] = (
    "syntara.core.database.session.AsyncSessionLocal",
    "syntara.api.main.AsyncSessionLocal",
    "syntara.audit.outbox.worker.AsyncSessionLocal",
    "syntara.audit.outbox.worker.AuditWorkerAsyncSessionLocal",
    "syntara.audit.outbox.session.AuditWorkerAsyncSessionLocal",
    "syntara.files.workers.file_cleanup.AsyncSessionLocal",
    "syntara.workflows.workers.schedule_reconciliation.AsyncSessionLocal",
    "syntara.metrics.completion_poller.AsyncSessionLocal",
)


@contextlib.contextmanager
def patched_test_db(engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]) -> Iterator[None]:
    """Patch every engine and session-factory binding to the test database.

    Shared by enter_test_lifespan and the session_app fixture so the two
    call sites cannot drift apart: a factory or engine binding missed here
    means startup code (or a worker singleton cached during startup) runs
    against the real database for the rest of the pytest-xdist worker.
    """
    with contextlib.ExitStack() as stack:
        for target in ENGINE_TARGETS:
            stack.enter_context(patch(target, engine))
        for target in SESSION_FACTORY_TARGETS:
            stack.enter_context(patch(target, session_factory))
        yield


def task_live_on_running_loop(task: asyncio.Task[None] | None) -> bool:
    """Return whether the task is live and bound to the loop this helper runs on.

    The lifespan tests run with loop_scope="session", so they share the loop a
    session-scoped session_app fixture runs its workers on. A worker left on
    some other loop (for example by a function-scoped test elsewhere in the
    suite) can still neither be awaited nor revived from here, and treating
    such tasks as "not running" keeps the restores below honest: the restore
    contract holds only when the test shares the worker's loop.
    """
    return task is not None and not task.done() and task.get_loop() is asyncio.get_running_loop()


def _outbox_live_on_other_loop() -> bool:
    """Return whether a cached outbox worker is live on a different event loop.

    Checking liveness without populating the cache (the currsize guard) keeps
    this from caching a worker bound to the real database factories.
    """
    if get_outbox_worker.cache_info().currsize == 0:
        return False
    task = get_outbox_worker()._task
    return task is not None and not task.done() and task.get_loop() is not asyncio.get_running_loop()


def skip_if_outbox_live_on_other_loop() -> None:
    """Skip when a session_app's outbox worker runs on the session loop.

    The helper cannot revive or stop a worker whose task belongs to another
    event loop (see task_live_on_running_loop), so a test asserting post-exit
    audit state would false-fail in that configuration: nested shutdown stops
    the worker, sets the audit state STOPPED, and the restore correctly skips
    the restart.
    """
    if _outbox_live_on_other_loop():
        pytest.skip("outbox worker running on another event loop (session_app live)")


def _singleton_worker_live_on_other_loop() -> bool:
    """Return whether any process-wide worker is live on a different event loop.

    Entering the lifespan stops all of these workers, and the helper can only
    restart the ones whose tasks belong to this test's loop, so a test that
    enters the lifespan must skip whenever any of them belongs to a live
    session_app. The lru_cache getters are inspected only when already
    populated, so this does not cache a worker bound to the real database.
    """
    loop = asyncio.get_running_loop()
    for getter in (get_outbox_worker, get_multipart_cleanup_worker, get_schedule_reconciliation_worker):
        if getter.cache_info().currsize == 0:
            continue
        task = getter()._task
        if task is not None and not task.done() and task.get_loop() is not loop:
            return True
    monitoring_task = get_connection_lifecycle_manager()._monitoring_task
    return monitoring_task is not None and not monitoring_task.done() and monitoring_task.get_loop() is not loop


def skip_if_singleton_workers_live_on_other_loop() -> None:
    """Skip when a session_app started any process-wide worker on the session loop."""
    if _singleton_worker_live_on_other_loop():
        pytest.skip("a session_app's worker is running on another event loop")


def _singleton_worker_running() -> bool:
    """Return whether any process-wide worker is live, on any event loop.

    The lru_cache getters are inspected only when already populated, so this
    does not cache a worker bound to the real database.
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
    """Skip when any process-wide worker is already live.

    Used by tests that stop the workers or drop their caches themselves rather
    than entering the lifespan. Lifespan tests run on the session loop (see
    enter_test_lifespan), so a live session_app's workers are stoppable from
    here, but a test that clears their caches or drives the reclaim path
    directly would strand them for the rest of the pytest-xdist worker.
    """
    if _singleton_worker_running():
        pytest.skip("a session_app's worker is already running")


def _restore_leaked_state_attrs(prior_state: dict[str, Any]) -> None:
    """Undo the app.state attributes the lifespan set at startup but never clears."""
    for name, prior in prior_state.items():
        if prior is _MISSING:
            if hasattr(app.state, name):
                delattr(app.state, name)
        else:
            setattr(app.state, name, prior)


def _capture_singleton_workers_running() -> tuple[bool, bool, bool]:
    """Capture whether the process-wide singleton workers run on this test's loop.

    The getters return process-wide singletons, so the instances a live
    session_app started are the same ones a nested lifespan shutdown stops.
    Only tasks live on the current test's event loop count as running (see
    task_live_on_running_loop); the per-call workers (metrics pollers, session
    cleanup, periodic collector) are recreated on every startup and need no
    capture.
    """
    ws_manager = get_connection_lifecycle_manager()
    ws_monitoring = task_live_on_running_loop(ws_manager._monitoring_task)
    multipart_running = task_live_on_running_loop(get_multipart_cleanup_worker()._task)
    schedule_running = task_live_on_running_loop(get_schedule_reconciliation_worker()._task)
    return ws_monitoring, multipart_running, schedule_running


async def stop_newly_started_workers(prior: tuple[bool, bool, bool]) -> None:
    """Stop singleton workers a failed startup left running.

    FastAPI's lifespan_context skips shutdown when startup raises, so a
    mid-startup failure after the workers start leaves the process-wide
    singletons running for the rest of the pytest-xdist worker. Stop each one
    that was stopped before entry but is running now. Per-call workers
    (metrics pollers, session cleanup, periodic collector) cannot be reclaimed
    this way: their getters return fresh instances nobody holds a reference to.
    """
    ws_monitoring, multipart_running, schedule_running = _capture_singleton_workers_running()
    prior_ws, prior_multipart, prior_schedule = prior
    if not prior_ws and ws_monitoring:
        with contextlib.suppress(Exception):
            get_connection_lifecycle_manager().stop_monitoring()
    if not prior_multipart and multipart_running:
        with contextlib.suppress(Exception):
            await get_multipart_cleanup_worker().stop()
    if not prior_schedule and schedule_running:
        with contextlib.suppress(Exception):
            await get_schedule_reconciliation_worker().stop()


def _restart_singleton_workers(running: tuple[bool, bool, bool]) -> None:
    """Restart the singleton workers this test's loop had running before entry.

    Only workers whose prior task ran on this test's event loop are restarted
    (see task_live_on_running_loop): a worker left on another loop cannot be
    revived from here, and creating a replacement would bind it to this test's
    loop. Each restart is
    suppressed so a failing restart cannot mask the logging restores that
    follow it (or the original test error).
    """
    ws_monitoring, multipart_running, schedule_running = running
    if ws_monitoring:
        with contextlib.suppress(Exception):
            get_connection_lifecycle_manager().start_monitoring()
    if multipart_running:
        with contextlib.suppress(Exception):
            get_multipart_cleanup_worker().start()
    if schedule_running:
        with contextlib.suppress(Exception):
            get_schedule_reconciliation_worker().start()


async def restore_audit_subsystems(
    prior_audit_state: audit_lifecycle.AuditLifecycleState,
    *,
    prior_outbox_on_loop: bool,
) -> None:
    """Restore the audit subsystems to how lifespan entry found them.

    When they were running before entry, restart the outbox worker: nested
    shutdown stopped it, and starting from nothing instead would leave the
    audit lifecycle RUNNING with a worker that a later
    get_outbox_worker.cache_clear() drops without stopping, after which the
    next start_audit_subsystems() would early-return without starting the
    replacement worker. The restart happens only when the prior worker task
    ran on this test's event loop (see task_live_on_running_loop): reviving a
    worker bound to another loop would bind the replacement to this test's
    loop.

    When the subsystems were stopped before entry, do nothing unless they are
    RUNNING now: startup failed midway after start_audit_subsystems() and the
    app's own shutdown never ran, so stop them here. Suppressed so a failure
    cannot mask the restores that follow.
    """
    if prior_audit_state is audit_lifecycle.AuditLifecycleState.RUNNING:
        if prior_outbox_on_loop:
            with contextlib.suppress(Exception):
                audit_lifecycle.start_audit_outbox_worker()
    elif audit_lifecycle._state is audit_lifecycle.AuditLifecycleState.RUNNING:
        with contextlib.suppress(Exception):
            await audit_lifecycle.stop_audit_subsystems()


def clear_caches_populated_by_entry(empty_at_entry: tuple[bool, bool, bool]) -> None:
    """Drop the singleton caches this entry populated.

    The cached workers are bound to this test's patched session factory, so
    the next caller must rebuild them against the restored module attributes.
    Caches that were already populated at entry belong to whatever populated
    them (e.g. a live session_app) and are preserved.
    """
    outbox_was_empty, multipart_was_empty, schedule_was_empty = empty_at_entry
    if outbox_was_empty:
        get_outbox_worker.cache_clear()
    if multipart_was_empty:
        get_multipart_cleanup_worker.cache_clear()
    if schedule_was_empty:
        get_schedule_reconciliation_worker.cache_clear()


@asynccontextmanager
async def enter_test_lifespan(test_db_engine: AsyncEngine) -> AsyncGenerator[None, None]:
    """Run the app lifespan with test database properly configured.

    Every entry stops the process-wide singleton workers and restarts them on
    exit, which only works when the test shares the workers' event loop. Tests
    that enter the lifespan declare loop_scope="session" for that reason: the
    session-scoped session_app fixture runs its workers on the session loop, so
    a session-loop test can stop and revive them like any other worker. The
    guard below still sits here, before any capture or startup work, rather
    than in each caller: a worker that is on some other loop (for example one a
    function-scoped test left behind) cannot be revived from here, and a caller
    that forgot to skip would leave it dead for the rest of the pytest-xdist
    worker.

    Args:
        test_db_engine: Test database engine with migrations applied

    """
    skip_if_singleton_workers_live_on_other_loop()

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
        patched_test_db(test_db_engine, test_session_factory),
        patch("syntara.api.main.RegoEvaluator", return_value=mock_evaluator),
    ):
        # Seed required data before app startup
        from syntara.core.seed import run_seeders

        await run_seeders(test_session_factory)

        # The lifespan sets each LEAKED_STATE_ATTRS entry at startup but never
        # clears it at shutdown; without restoring the prior values the test's
        # instances leak to every later test in this pytest-xdist worker.
        prior_state: dict[str, Any] = {name: getattr(app.state, name, _MISSING) for name in LEAKED_STATE_ATTRS}

        # The lifespan also mutates process-global state beyond app.state: it
        # starts the audit subsystems, replaces the process-wide runtime settings
        # cache, appends a duplicate route set to the shared app, stops the
        # process-wide singleton workers, and removes every root logger handler.
        # A session-scoped session_app fixture relies on all of these for the
        # rest of this worker, so capture them here and restore them on exit.
        # Every worker-liveness capture is loop-aware: a restore performed from
        # this function-scoped test only holds when the worker's task shares
        # this test's event loop.
        prior_runtime_settings = settings_cache._runtime_settings
        prior_audit_state = audit_lifecycle._state
        # The lru_cache capture calls below (and the nested lifespan startup
        # itself) populate the process-wide singleton caches when empty, binding
        # the workers to this test's patched session factory. Record which
        # caches were empty at entry so the finally block can clear them: a
        # cached worker bound to a disposed test engine outlives this test
        # until the next session_app cache_clear (the PR #921 leak class).
        # Getters already populated at entry (e.g. by a live session_app) are
        # left untouched; get_connection_lifecycle_manager is a plain
        # module-global with no factory binding and needs no such handling.
        prior_caches_empty = (
            get_outbox_worker.cache_info().currsize == 0,
            get_multipart_cleanup_worker.cache_info().currsize == 0,
            get_schedule_reconciliation_worker.cache_info().currsize == 0,
        )
        prior_outbox_on_loop = task_live_on_running_loop(get_outbox_worker()._task)
        prior_logging_state = logging_lifecycle._logging_state
        prior_routes = list(app.router.routes)
        # build_websocket_router() appends a ValidationInterceptor to the
        # process-global registry on every startup with no dedup guard, so a
        # session_app's bootstrap callbacks would run every retained validator.
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
        prior_singletons = _capture_singleton_workers_running()

        # Run the app lifespan
        try:
            async with app.router.lifespan_context(app):
                yield
        finally:
            _restore_leaked_state_attrs(prior_state)
            # Restore the process-wide runtime settings cache: nested startup replaced
            # it and nested shutdown only stopped the nested cache's watcher, which
            # would leave later tests reading settings through an orphaned cache.
            settings_cache._runtime_settings = prior_runtime_settings
            # Undo the duplicate route registration: router discovery and the manual
            # WebSocket router include have no dedup guard, so every entry appends a
            # full route set to the singleton app for the rest of this worker.
            app.router.routes[:] = prior_routes
            get_registry()._interceptors[:] = prior_interceptors
            await restore_audit_subsystems(prior_audit_state, prior_outbox_on_loop=prior_outbox_on_loop)
            await stop_newly_started_workers(prior_singletons)
            _restart_singleton_workers(prior_singletons)
            clear_caches_populated_by_entry(prior_caches_empty)
            # Restore the logging subsystems directly rather than restarting them via
            # start_loggers(): it installs fresh stdout/OTLP handlers (duplicating the
            # prior ones) and cannot restore handlers the lifespan never owned, such
            # as the per-worker log file handler the session_app fixture attaches.
            root_logger.handlers[:] = prior_root_handlers
            root_logger.setLevel(prior_root_level)
            for name, level in prior_uvicorn_levels.items():
                logging.getLogger(name).setLevel(level)
            audit_logger.handlers[:] = prior_audit_log_handlers
            audit_logger.setLevel(prior_audit_log_level)
            audit_logger.propagate = prior_audit_log_propagate
            logging_lifecycle._logging_state = prior_logging_state
