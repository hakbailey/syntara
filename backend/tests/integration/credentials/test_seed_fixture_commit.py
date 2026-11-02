"""Regression test for the autouse group-seed fixture's commit contract.

``_seed_authenticated_group`` in ``tests/integration/conftest.py`` seeds the
authenticated group and must commit it. An uncommitted insert holds a
RowExclusiveLock on ``groups`` for the rest of the test, and ``run_seeders``'
audit seeder issues ``DROP TRIGGER ... ON groups``, which needs an
AccessExclusiveLock on the same table, so any lifespan-entering test in a
directory without a committing fixture deadlocked.

The lifespan-entry regression test skips whenever a session-scoped session_app
owns the process-wide workers, which is common in a full pytest-xdist run. This
pin enters no lifespan and therefore runs in every configuration: it reads the
seeded row back over a separate connection, which only succeeds once the
fixture's insert is committed.
"""

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from syntara.authz.resolver import AUTHENTICATED_GROUP_NAME
from syntara.core.models.group import Group


async def test_seed_authenticated_group_fixture_commits(test_db_engine: AsyncEngine) -> None:
    """The seeded authenticated group must be visible outside the fixture's session."""
    async with AsyncSession(test_db_engine) as probe:
        result = await probe.exec(select(Group).where(Group.name == AUTHENTICATED_GROUP_NAME))
        assert result.first() is not None, (
            "the autouse _seed_authenticated_group fixture left its insert uncommitted: "
            "another connection cannot see the row, and the open transaction keeps a lock "
            "on groups that run_seeders' DROP TRIGGER cannot take"
        )
