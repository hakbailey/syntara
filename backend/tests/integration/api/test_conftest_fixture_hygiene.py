"""Regression test for the api conftest's authz evaluator override cleanup.

``_mock_evaluator`` in ``tests/integration/api/conftest.py``
sets ``app.dependency_overrides[get_authz_evaluator]`` and never pops it at
teardown; the root conftest overwrites the entry per test, which works only by
convention. The fixture must remove its own override during teardown.

The test drives the fixture function directly with a private MonkeyPatch so the
teardown runs inside the test: after undoing that MonkeyPatch, the override
entry must be gone. pytest 9 forbids calling a fixture object directly, so the
underlying function is reached through its ``__wrapped__`` attribute.
"""

from typing import TYPE_CHECKING, cast

import pytest

from syntara.api.main import app
from syntara.authz.dependencies import get_authz_evaluator
from tests.integration.api.conftest import _mock_evaluator

if TYPE_CHECKING:
    from collections.abc import Callable


def _drive_fixture(fixture: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invoke the plain function behind a pytest fixture decoration."""
    raw = getattr(fixture, "__wrapped__", None)
    assert raw is not None
    cast("Callable[[pytest.MonkeyPatch], None]", raw)(monkeypatch)


def test_mock_evaluator_pops_dependency_override_on_teardown() -> None:
    """_mock_evaluator must remove its dependency_overrides entry at teardown."""
    saved_overrides = dict(app.dependency_overrides)
    # Clear first so the assertion observes only the driven fixture's entry: the
    # autouse _mock_evaluator already set this key for THIS test.
    app.dependency_overrides.clear()
    monkeypatch = pytest.MonkeyPatch()
    try:
        _drive_fixture(_mock_evaluator, monkeypatch)
        assert get_authz_evaluator in app.dependency_overrides
        monkeypatch.undo()
        assert get_authz_evaluator not in app.dependency_overrides, (
            "_mock_evaluator left its dependency_overrides entry behind; only the root "
            "conftest overwriting the same key per test keeps this harmless"
        )
    finally:
        monkeypatch.undo()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved_overrides)
