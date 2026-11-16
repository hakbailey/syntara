"""Shared installer for the authz evaluator mock used by integration conftests."""

import importlib
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from syntara.authz.dependencies import get_authz_evaluator
from tests.integration.helpers.router_modules import ROUTER_MODULES


def install_authz_evaluator_mock(monkeypatch: pytest.MonkeyPatch, evaluator: AsyncMock) -> None:
    """Patch get_authz_evaluator everywhere the routers can bind it.

    Replaces the module attribute on the definition module plus every router
    module that binds get_authz_evaluator (derived from the same source of
    truth the collection-time import loop uses, so a newly added router cannot
    be missed), and registers the dependency_overrides entry through
    monkeypatch so it is popped at teardown instead of relying on another
    conftest overwriting the same key in the next test.

    Every target is imported before the first patch is installed. A router
    module first imported while the definition module is patched would capture
    the test-defined getter in Depends(...) permanently, and later
    dependency_overrides keyed on the real function would never match for its
    routes. Importing first makes the helper self-contained; the integration
    conftest's collection-time import loop remains as defence in depth.

    get_authz_evaluator is imported at this module's import time (collection,
    before any patch can run), so the override is always keyed on the real
    function object even when the definition module's attribute is patched.
    The app is imported lazily inside this function so importing this module
    (which both integration conftests load at collection time) does not pull in
    syntara.api.main on its own.
    """
    from syntara.api.main import app

    def _mock_getter(request: Any = None) -> AsyncMock:  # noqa: ANN401
        return evaluator

    # Import every target first: a router module imported after the first
    # setattr below would capture the patched getter in Depends(...) for good.
    modules = [importlib.import_module(target) for target in ("syntara.authz.dependencies", *ROUTER_MODULES)]
    for module in modules:
        # ROUTER_MODULES is derived from a scan for the identifier, so it also
        # lists modules that only reach the evaluator through the definition
        # module (for example syntara.authz.dependencies.get_authz_evaluator).
        # Those never bind the bare name, and monkeypatch.setattr's default
        # raising=True would fail on them; the definition module's patch
        # already covers their lookups.
        if not hasattr(module, "get_authz_evaluator"):
            continue
        monkeypatch.setattr(module, "get_authz_evaluator", _mock_getter)

    # The cast to a plain object-keyed dict works around mypy being unable to
    # infer setitem's type parameters from Callable-keyed dicts.
    overrides = cast("dict[object, object]", app.dependency_overrides)
    monkeypatch.setitem(overrides, get_authz_evaluator, lambda: evaluator)
