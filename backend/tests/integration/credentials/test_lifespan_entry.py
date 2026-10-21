"""Regression test for entering the app lifespan from a non-api directory.

The autouse ``_seed_authenticated_group`` fixture in
``tests/integration/conftest.py`` used to insert and flush without committing,
holding a RowExclusiveLock on ``groups`` for the whole test, while
``run_seeders``' ``DROP TRIGGER`` needs an AccessExclusiveLock on the same
table, so any lifespan-entering test in a directory without a committing
fixture deadlocked. The fixture now commits unconditionally (see
``tests/integration/conftest.py``); this test guards that contract by entering
the lifespan from a non-api directory.
"""

import asyncio

from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.helpers.lifespan import _test_lifespan_context


async def test_lifespan_entry_completes_outside_api_directory(test_db_engine: AsyncEngine) -> None:
    """Entering the app lifespan from any test directory must complete."""
    # The guarded deadlock manifests as an indefinite hang inside run_seeders, so the
    # timeout is the failure oracle: on regression this fails with TimeoutError at the
    # audit seeder's DROP TRIGGER instead of hanging the worker.
    async with asyncio.timeout(120), _test_lifespan_context(test_db_engine):
        pass
