"""Integration tests for application lifecycle - audit system startup and shutdown.

This suite lives in its own directory so the make targets can run it in its own
pytest process (``make test-lifespan``); see the lifespan helper for why it must
not share a process with the session-scoped session_app fixture.
"""

import ast
import asyncio
import importlib
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

import syntara
from syntara.api.main import app
from syntara.audit.dispatcher import AuditEventDispatcher
from syntara.audit.outbox import session as outbox_session
from syntara.audit.outbox import worker as outbox_worker
from syntara.audit.outbox.session import AuditWorkerAsyncSessionLocal
from syntara.audit.outbox.worker import get_outbox_worker
from syntara.core.database.session import AsyncSessionLocal
from syntara.core.websocket.manager import (
    WebSocketConnectionLifecycleManager,
    get_connection_lifecycle_manager,
)
from syntara.core.workers.periodic import PeriodicWorker
from syntara.files.workers.file_cleanup import get_multipart_cleanup_worker
from syntara.workflow_runtime.workers.schedule_reconciliation import get_schedule_reconciliation_worker
from tests.integration.helpers.lifespan import (
    ENGINE_TARGETS,
    LEAKED_STATE_ATTRS,
    SESSION_FACTORY_TARGETS,
    UVICORN_LOGGERS,
    enter_test_lifespan,
    patched_test_db,
    skip_if_singleton_workers_running,
    stop_leftover_audit_subsystems,
    stop_leftover_workers,
    task_live_on_running_loop,
)


@pytest.mark.asyncio(loop_scope="session")
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


@pytest.mark.asyncio(loop_scope="session")
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


@pytest.mark.asyncio(loop_scope="session")
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


@pytest.mark.asyncio(loop_scope="session")
async def test_lifespan_restores_prior_app_state_attrs(test_db_engine: AsyncEngine) -> None:
    """Lifespan entry must leave each leaked app.state attribute as it found it.

    The lifespan sets every LEAKED_STATE_ATTRS entry at startup and never
    clears it at shutdown, so a lifespan-entering test leaves its instances
    behind for every later test in the same pytest-xdist worker. Production
    code reads these attributes directly at request time, so a leaked mock or
    test-bound instance changes later tests' behavior.

    Both prior-state phases (attribute present and attribute absent) are
    asserted inside a single lifespan entry each, rather than one entry per
    attribute per phase, to keep the number of full lifespan boots at two.

    The test itself must honor the same contract: enter_test_lifespan skips
    when a process-wide worker is already live, because it cannot revive one
    it did not start.
    """
    originals = {name: (hasattr(app.state, name), getattr(app.state, name, None)) for name in LEAKED_STATE_ATTRS}
    try:
        sentinels = {name: object() for name in LEAKED_STATE_ATTRS}
        for name, sentinel in sentinels.items():
            setattr(app.state, name, sentinel)
        async with asyncio.timeout(120), enter_test_lifespan(test_db_engine):
            for name, sentinel in sentinels.items():
                assert getattr(app.state, name) is not sentinel, (
                    f"app.state.{name} was not replaced at lifespan startup"
                )
        for name, sentinel in sentinels.items():
            assert getattr(app.state, name) is sentinel, (
                f"app.state.{name} was not restored after lifespan shutdown; "
                "the lifespan test's instance leaks to later tests in this worker"
            )

        # Every attribute holds its sentinel here, so the deletes cannot miss.
        for name in LEAKED_STATE_ATTRS:
            delattr(app.state, name)
        async with asyncio.timeout(120), enter_test_lifespan(test_db_engine):
            for name in LEAKED_STATE_ATTRS:
                assert hasattr(app.state, name), f"app.state.{name} was not set at lifespan startup"
        for name in LEAKED_STATE_ATTRS:
            assert not hasattr(app.state, name), (
                f"app.state.{name} was absent before lifespan entry but present after "
                "shutdown; the lifespan test's instance leaks to later tests in this worker"
            )
    finally:
        _restore_original_state_attrs(originals)


def _restore_original_state_attrs(originals: dict[str, tuple[bool, Any]]) -> None:
    """Put each app.state attribute back to its pre-test value."""
    for name, (had_original, original) in originals.items():
        if had_original:
            setattr(app.state, name, original)
        elif hasattr(app.state, name):
            delattr(app.state, name)


@pytest.mark.asyncio(loop_scope="session")
async def test_lifespan_restores_app_state_attribute_set(test_db_engine: AsyncEngine) -> None:
    """No app.state attribute the lifespan sets may survive helper exit.

    LEAKED_STATE_ATTRS is hand-maintained; if the production lifespan starts
    setting (or deleting) a new app.state attribute it never clears, the
    restore test above cannot notice because it only covers the listed names.
    Diffing the full attribute-name set across an entry makes the tuple
    self-verifying: any attribute the lifespan sets and never clears fails
    this test whether or not it is listed.
    """
    prior_keys = sorted(app.state)
    async with asyncio.timeout(120), enter_test_lifespan(test_db_engine):
        pass
    assert sorted(app.state) == prior_keys, (
        "app.state gained or lost attributes across lifespan entry; update "
        "LEAKED_STATE_ATTRS in tests/integration/helpers/lifespan.py to cover the leak"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_lifespan_leaves_process_global_state_as_found(test_db_engine: AsyncEngine) -> None:
    """Lifespan entry must restore the process-global state it disrupts.

    Entering the lifespan starts the audit subsystems, replaces the process-wide
    runtime settings cache, and its shutdown stops the subsystems and removes every
    root logger handler. The tests in this file boot the lifespan repeatedly in
    one process, so enter_test_lifespan must leave each one as it found it. In
    particular, restarting the audit subsystems when nothing was running before
    the entry would leave the audit lifecycle RUNNING with an outbox worker that
    a later get_outbox_worker.cache_clear() drops without stopping.
    """
    import logging

    import syntara.audit.lifecycle as audit_lifecycle
    from syntara.audit.logging import AUDIT_LOGGER_NAME
    from syntara.core.logging import lifecycle as logging_lifecycle
    from syntara.core.websocket.interceptor import get_registry
    from syntara.settings.cache import settings_cache

    prior_audit_state = audit_lifecycle._state
    prior_logging_state = logging_lifecycle._logging_state
    prior_runtime_settings = settings_cache._runtime_settings
    prior_handlers = list(logging.getLogger().handlers)
    prior_audit_handlers = list(logging.getLogger(AUDIT_LOGGER_NAME).handlers)
    prior_root_level = logging.getLogger().level
    prior_uvicorn_levels = {name: logging.getLogger(name).level for name in UVICORN_LOGGERS}
    prior_audit_level = logging.getLogger(AUDIT_LOGGER_NAME).level
    prior_audit_propagate = logging.getLogger(AUDIT_LOGGER_NAME).propagate
    prior_interceptors = list(get_registry()._interceptors)

    # A cross-loop worker stop would hang the xdist worker instead of failing,
    # so the timeout is the failure oracle for that direction.
    async with asyncio.timeout(120), enter_test_lifespan(test_db_engine):
        assert audit_lifecycle._state is audit_lifecycle.AuditLifecycleState.RUNNING

    # Collect every mismatch before asserting so one failed restore does not
    # hide the state of the others; each entry is (passed, failure message).
    checks: list[tuple[bool, str]] = [
        (
            audit_lifecycle._state is prior_audit_state,
            "audit lifecycle state leaked past lifespan exit; restarting when nothing was "
            "running leaves an orphaned outbox worker behind for the rest of this worker",
        )
    ]
    checks.extend(
        [
            (
                logging_lifecycle._logging_state is prior_logging_state,
                "logging lifecycle state leaked past lifespan exit",
            ),
            (
                settings_cache._runtime_settings is prior_runtime_settings,
                "the process-wide runtime settings cache was replaced by the nested lifespan and "
                "not restored; later tests would read settings through the orphaned cache",
            ),
            (
                logging.getLogger().handlers == prior_handlers,
                "root logger handlers present before lifespan entry were not restored after "
                "shutdown; handlers the lifespan never owned are lost for the rest of this "
                "process",
            ),
            (
                logging.getLogger(AUDIT_LOGGER_NAME).handlers == prior_audit_handlers,
                "audit logger handlers present before lifespan entry were not restored after "
                "shutdown; audit handlers the lifespan never owned are lost for the rest of this "
                "process",
            ),
            (
                logging.getLogger().level == prior_root_level,
                "root logger level present before lifespan entry was not restored after shutdown; "
                "the nested lifespan's fallback log level leaks to the rest of this worker",
            ),
            (
                logging.getLogger(AUDIT_LOGGER_NAME).level == prior_audit_level,
                "audit logger level present before lifespan entry was not restored after shutdown; "
                "configure_audit_logging()'s NOTSET level leaks to the rest of this worker",
            ),
            (
                logging.getLogger(AUDIT_LOGGER_NAME).propagate == prior_audit_propagate,
                "audit logger propagate flag present before lifespan entry was not restored after "
                "shutdown; configure_audit_logging() forcing it to False changes how later audit "
                "records reach the root logger and pytest logging capture",
            ),
            (
                get_registry()._interceptors == prior_interceptors,
                "interceptors registered before lifespan entry were not restored after shutdown; "
                "build_websocket_router() appends a ValidationInterceptor per entry with no dedup "
                "guard, so later bootstraps run every retained validator",
            ),
        ]
    )
    for name in UVICORN_LOGGERS:
        checks.append(
            (
                logging.getLogger(name).level == prior_uvicorn_levels[name],
                f"{name} logger level present before lifespan entry was not restored after "
                "shutdown; apply_runtime_log_level() sets it on every lifespan entry",
            )
        )
    failed = [message for passed, message in checks if not passed]
    assert not failed, "\n".join(failed)


def _start_singletons_on_this_loop(
    test_db_engine: AsyncEngine,
) -> tuple[WebSocketConnectionLifecycleManager, PeriodicWorker, PeriodicWorker, bool, bool, bool]:
    """Start the three singleton workers on this test's event loop if idle.

    Returns the manager, the two periodic workers, and which of the three this
    call started (so callers stop only what they started). The two lru_cache
    workers are created inside factory patches so a fresh cache entry binds to
    the test database instead of the real one.
    """
    test_session_factory = async_sessionmaker(test_db_engine, class_=AsyncSession, expire_on_commit=False)
    with patched_test_db(test_db_engine, test_session_factory):
        ws_manager = get_connection_lifecycle_manager()
        multipart_worker = get_multipart_cleanup_worker()
        schedule_worker = get_schedule_reconciliation_worker()

        started_ws = not task_live_on_running_loop(ws_manager._monitoring_task)
        if started_ws:
            ws_manager.start_monitoring()
        started_multipart = not task_live_on_running_loop(multipart_worker._task)
        if started_multipart:
            multipart_worker.start()
        started_schedule = not task_live_on_running_loop(schedule_worker._task)
        if started_schedule:
            schedule_worker.start()

    return ws_manager, multipart_worker, schedule_worker, started_ws, started_multipart, started_schedule


@pytest.mark.asyncio(loop_scope="session")
async def test_lifespan_clears_singleton_caches_it_populated(test_db_engine: AsyncEngine) -> None:
    """Helper exit must not leave factory-patched singleton workers cached.

    The lifespan startup populates the process-wide lru_cache getters; the
    cached workers are then bound to this test's patched session factory and
    outlive the test with a disposed engine (the PR #921 leak class). The
    helper must clear them so the next entry rebuilds against the restored
    module attributes.
    """
    get_outbox_worker.cache_clear()
    get_multipart_cleanup_worker.cache_clear()
    get_schedule_reconciliation_worker.cache_clear()
    try:
        # A cross-loop worker stop would hang the xdist worker instead of
        # failing, so the timeout is the failure oracle for that direction.
        async with asyncio.timeout(120), enter_test_lifespan(test_db_engine):
            assert get_outbox_worker.cache_info().currsize > 0, (
                "expected the helper's capture or the nested startup to populate the outbox cache"
            )

        for getter, label in (
            (get_outbox_worker, "outbox"),
            (get_multipart_cleanup_worker, "multipart cleanup"),
            (get_schedule_reconciliation_worker, "schedule reconciliation"),
        ):
            assert getter.cache_info().currsize == 0, (
                f"the {label} worker cached by this entry was not cleared at exit; the "
                "cached worker is bound to this test's session factory and outlives its engine"
            )

        # The next caller must rebuild against the restored module attributes,
        # not this test's patched factory. Compare against the import-time
        # bindings rather than the module attributes the getter itself reads:
        # those are patched together with the worker, so comparing with them
        # passes even when a patch leaked.
        rebuilt = get_outbox_worker()
        assert rebuilt._session_factory is AuditWorkerAsyncSessionLocal, (
            "get_outbox_worker() rebuilt the worker against a factory other than the "
            "import-time AuditWorkerAsyncSessionLocal; a patched factory leaked past exit"
        )
        assert rebuilt._write_session_factory is AsyncSessionLocal, (
            "get_outbox_worker() rebuilt the worker with a write factory other than the "
            "import-time AsyncSessionLocal; a patched factory leaked past exit"
        )
    finally:
        get_outbox_worker.cache_clear()
        get_multipart_cleanup_worker.cache_clear()
        get_schedule_reconciliation_worker.cache_clear()


@pytest.mark.asyncio(loop_scope="session")
async def test_stop_leftover_workers_stops_failed_startup_workers(test_db_engine: AsyncEngine) -> None:
    """stop_leftover_workers must stop singletons a failed startup left running.

    FastAPI's lifespan_context skips shutdown when startup raises, so the
    helper's finally cannot rely on nested shutdown having run. No other test
    exercises this reclaim path (triggering a real mid-startup failure is not
    practical), so it is pinned directly here.
    """
    skip_if_singleton_workers_running()
    multipart_was_cached = get_multipart_cleanup_worker.cache_info().currsize > 0
    schedule_was_cached = get_schedule_reconciliation_worker.cache_info().currsize > 0
    ws_manager, multipart_worker, schedule_worker, started_ws, started_multipart, started_schedule = (
        _start_singletons_on_this_loop(test_db_engine)
    )
    try:
        await stop_leftover_workers()
        assert not task_live_on_running_loop(ws_manager._monitoring_task), (
            "websocket monitoring a failed startup left running was not stopped"
        )
        assert not task_live_on_running_loop(multipart_worker._task), (
            "multipart cleanup worker a failed startup left running was not stopped"
        )
        assert not task_live_on_running_loop(schedule_worker._task), (
            "schedule reconciliation worker a failed startup left running was not stopped"
        )
    finally:
        if started_ws:
            ws_manager.stop_monitoring()
        if started_multipart:
            await multipart_worker.stop()
        if started_schedule:
            await schedule_worker.stop()
        # This test may have populated the lru_caches with test-factory-bound
        # workers; drop them so later callers rebuild cleanly.
        if not multipart_was_cached:
            get_multipart_cleanup_worker.cache_clear()
        if not schedule_was_cached:
            get_schedule_reconciliation_worker.cache_clear()


def _audit_subsystems_running() -> bool:
    """Read the audit lifecycle state as a plain bool."""
    import syntara.audit.lifecycle as audit_lifecycle

    return audit_lifecycle._state is audit_lifecycle.AuditLifecycleState.RUNNING


@pytest.mark.asyncio(loop_scope="session")
async def test_stop_leftover_audit_subsystems_stops_failed_startup_subsystems(test_db_engine: AsyncEngine) -> None:
    """stop_leftover_audit_subsystems must stop subsystems a failed startup left RUNNING.

    The stop branch only executes when the lifespan started the audit
    subsystems and then raised before shutdown could run (FastAPI's
    lifespan_context skips shutdown on startup failure). No other test pins
    this branch: deleting it passes the entire suite. It is driven directly
    here, mirroring test_stop_leftover_workers_stops_failed_startup_workers,
    because triggering a real mid-startup failure is not practical.
    """
    import logging

    import syntara.audit.lifecycle as audit_lifecycle
    import syntara.core.logging.lifecycle as logging_lifecycle
    from syntara.audit.logging import AUDIT_LOGGER_NAME

    outbox_was_cached = get_outbox_worker.cache_info().currsize > 0
    was_running = audit_lifecycle._state is audit_lifecycle.AuditLifecycleState.RUNNING
    test_session_factory = async_sessionmaker(test_db_engine, class_=AsyncSession, expire_on_commit=False)
    # The stop branch calls stop_audit_subsystems(), which removes every root
    # and audit handler and sets _logging_state to UNCONFIGURED. The helper
    # normally restores logging after this call; a direct drive must do it
    # itself or later tests in this worker lose their handlers.
    root_logger = logging.getLogger()
    audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
    prior_logging = (
        list(root_logger.handlers),
        root_logger.level,
        list(audit_logger.handlers),
        audit_logger.level,
        audit_logger.propagate,
        logging_lifecycle._logging_state,
    )
    started_here = False
    with patched_test_db(test_db_engine, test_session_factory):
        if not was_running:
            audit_lifecycle.start_audit_outbox_worker()
            started_here = True

    try:
        # Subsystems RUNNING after a failed startup is exactly the state the
        # helper must reclaim.
        await stop_leftover_audit_subsystems()
        assert audit_lifecycle._state is audit_lifecycle.AuditLifecycleState.STOPPED, (
            "stop_leftover_audit_subsystems did not stop the audit subsystems a failed startup "
            "left RUNNING; they keep running for the rest of this process"
        )
    finally:
        # Put the worker back if it was running before this test, so the
        # direct drive does not leak a stopped audit lifecycle to later tests.
        # Read through a helper: the assert above narrows _state for mypy,
        # which would flag the direct identity check as non-overlapping.
        if was_running and not _audit_subsystems_running():
            audit_lifecycle.start_audit_outbox_worker()
        # Stop the worker this test started even when the branch under test
        # failed to: clearing the cache below would otherwise drop it without
        # stopping the running task.
        if started_here and _audit_subsystems_running():
            await audit_lifecycle.stop_audit_outbox_worker()
        if started_here and not outbox_was_cached:
            get_outbox_worker.cache_clear()
        root_logger.handlers[:] = prior_logging[0]
        root_logger.setLevel(prior_logging[1])
        audit_logger.handlers[:] = prior_logging[2]
        audit_logger.setLevel(prior_logging[3])
        audit_logger.propagate = prior_logging[4]
        logging_lifecycle._logging_state = prior_logging[5]


def _modules_binding_at_import(name: str) -> set[str]:
    """Syntara modules whose module-level imports bind ``name`` from the session module."""
    package_root = Path(syntara.__path__[0])
    found: set[str] = set()
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.module != "syntara.core.database.session":
                continue
            if any(alias.name == name for alias in node.names):
                relative = path.relative_to(package_root.parent).with_suffix("")
                found.add(relative.as_posix().replace("/", "."))
    return found


def test_db_factory_targets_match_import_time_bindings() -> None:
    """The DB-factory patch targets must track the modules that bind at import time.

    SESSION_FACTORY_TARGETS and ENGINE_TARGETS in helpers/lifespan.py exist
    because some modules bind AsyncSessionLocal or engine at import, so
    patching the syntara.core.database.session module attributes never reaches
    them. Two directions are pinned here: a target whose module no longer
    binds at import is stale, and the workers the lifespan starts must be
    covered: the lru_cache singletons keep their cached worker for the rest of
    the worker (the PR #921 leak class), and the per-call cleanup workers read
    the module attribute every run, so both need their module patched.

    Many modules outside the lifespan's reach also bind AsyncSessionLocal at
    import (Temporal activities, pollers, CLI entry points); they are
    deliberately not patched because nothing the lifespan starts reads them
    under test, so this guard checks only the listed targets rather than
    demanding every import-time binding be present.
    """
    session_bound = _modules_binding_at_import("AsyncSessionLocal")
    engine_bound = _modules_binding_at_import("engine")

    # Resolve each target's attribute at runtime rather than filtering on the
    # ".AsyncSessionLocal" suffix: the AuditWorkerAsyncSessionLocal targets have
    # a different attribute name, and syntara.audit.outbox.session defines it
    # rather than importing it, so both checks have to cover it.
    stale_factory: list[str] = []
    for target in SESSION_FACTORY_TARGETS:
        module_name, _, attribute = target.rpartition(".")
        if module_name == "syntara.core.database.session":
            continue
        if not hasattr(importlib.import_module(module_name), attribute):
            stale_factory.append(target)
    stale_factory.sort()
    assert not stale_factory, (
        "SESSION_FACTORY_TARGETS lists targets whose modules no longer bind the attribute "
        "at import time; remove them from "
        f"tests/integration/helpers/lifespan.py: {stale_factory}"
    )

    engine_target_modules = {target.removesuffix(".engine") for target in ENGINE_TARGETS} - {
        "syntara.core.database.session"
    }
    stale_engine = sorted(engine_target_modules - engine_bound)
    assert not stale_engine, (
        "ENGINE_TARGETS lists engine targets whose modules no longer bind engine at "
        f"import time; remove them from tests/integration/helpers/lifespan.py: {stale_engine}"
    )

    for worker_module in (
        "syntara.audit.outbox.worker",
        "syntara.files.workers.file_cleanup",
        "syntara.workflow_runtime.workers.schedule_reconciliation",
        # Not cached singletons, but their callbacks read the module attribute
        # on every run, so they need the same patch to reach the test database.
        "syntara.metrics.cleanup",
        "syntara.auth.session.cleanup",
    ):
        assert f"{worker_module}.AsyncSessionLocal" in SESSION_FACTORY_TARGETS, (
            f"{worker_module} binds AsyncSessionLocal at import time and its lru_cache "
            "singleton is started by the lifespan; add it to SESSION_FACTORY_TARGETS in "
            "tests/integration/helpers/lifespan.py or its cached worker binds to the real "
            "database"
        )
        assert worker_module in session_bound, (
            f"{worker_module} no longer binds AsyncSessionLocal at import time; its patch target needs revisiting"
        )
