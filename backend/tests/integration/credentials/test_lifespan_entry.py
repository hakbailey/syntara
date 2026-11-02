"""Regression test for entering the app lifespan from a non-api directory.

The autouse ``_seed_authenticated_group`` fixture in
``tests/integration/conftest.py`` used to insert and flush without committing,
holding a RowExclusiveLock on ``groups`` for the whole test, while
``run_seeders``' ``DROP TRIGGER`` needs an AccessExclusiveLock on the same
table, so any lifespan-entering test in a directory without a committing
fixture deadlocked. The fixture now commits unconditionally (see
``tests/integration/conftest.py``); this test guards that contract by entering
the lifespan from a non-api directory.

A session-scoped session_app can also be live in the same pytest-xdist worker.
This test runs with ``loop_scope="session"`` so it shares that fixture's event
loop, which lets ``enter_test_lifespan`` stop and restart the session app's
workers like any other worker instead of skipping.
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

import syntara.audit.lifecycle as audit_lifecycle
from tests.integration.helpers.lifespan import enter_test_lifespan


@pytest.mark.asyncio(loop_scope="session")
async def test_lifespan_entry_completes_outside_api_directory(test_db_engine: AsyncEngine) -> None:
    """Entering the app lifespan from any test directory must complete."""
    # The guarded deadlock manifests as an indefinite hang inside run_seeders, so the
    # timeout is the failure oracle: on regression this fails with TimeoutError at the
    # audit seeder's DROP TRIGGER instead of hanging the worker.
    async with asyncio.timeout(120), enter_test_lifespan(test_db_engine):
        # Without this assertion the timeout oracle is vacuous on the green path: if
        # enter_test_lifespan ever stopped entering the lifespan, the test would
        # pass without exercising the seeded-entry path at all.
        assert audit_lifecycle._state is audit_lifecycle.AuditLifecycleState.RUNNING, (
            "lifespan entry did not start the audit subsystems; the timeout above "
            "would pass vacuously if the lifespan were no longer entered"
        )
