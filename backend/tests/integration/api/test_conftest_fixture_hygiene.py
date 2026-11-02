"""Regression tests for the conftest authz evaluator override cleanup.

Both conftest mock fixtures (``_mock_evaluator`` in
``tests/integration/api/conftest.py`` and ``_mock_evaluator_allow_all`` in
``tests/integration/conftest.py``) register their
``app.dependency_overrides[get_authz_evaluator]`` entry through
``install_authz_evaluator_mock`` and monkeypatch, so the entry is popped at
teardown; leaving it behind works only because the other conftest overwrites
the same key per test.

The installer is driven directly with a private MonkeyPatch so the teardown
runs inside the test: after undoing that MonkeyPatch, the override entry must
be gone. This pins exactly the mechanism both fixtures delegate to, without
reaching into pytest fixture internals (``__wrapped__``).
"""

from unittest.mock import AsyncMock

import pytest

from syntara.api.main import app
from syntara.authz.dependencies import get_authz_evaluator
from tests.integration.helpers.authz import install_authz_evaluator_mock


def test_install_authz_evaluator_mock_pops_dependency_override_on_undo() -> None:
    """The installer's dependency_overrides entry must be removed at MonkeyPatch undo."""
    saved_overrides = dict(app.dependency_overrides)
    # Clear first: the autouse _mock_evaluator already set this key for THIS test, and
    # setitem-based teardown restores the pre-setitem value rather than deleting, so the
    # entry must be absent before the installer runs for the undo to remove it.
    app.dependency_overrides.clear()
    monkeypatch = pytest.MonkeyPatch()
    try:
        install_authz_evaluator_mock(monkeypatch, AsyncMock())
        # The override is keyed on the real function: the helper module imports
        # get_authz_evaluator at collection time, before any patch can replace
        # the definition module's attribute.
        assert get_authz_evaluator in app.dependency_overrides
        monkeypatch.undo()
        assert get_authz_evaluator not in app.dependency_overrides, (
            "install_authz_evaluator_mock left its dependency_overrides entry behind; "
            "only the other conftest overwriting the same key per test keeps this harmless"
        )
    finally:
        monkeypatch.undo()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved_overrides)
