"""Regression tests for router modules binding get_authz_evaluator at import time.

Router modules capture ``get_authz_evaluator`` inside
``Depends(...)`` when the module is first imported, and the app registers
routers during lifespan startup rather than at import. Whichever test triggers
the first lifespan entry in a pytest-xdist worker therefore decides which
monkeypatch is live when the router modules are first imported. If that import
happens while this directory's conftest patch is active, every router binds the
patched getter permanently, and later tests' ``dependency_overrides`` (keyed on
the real function object) never match: credential list visibility then
resolves through regopy with no policies and returns empty lists.

These tests import the router modules from inside the test body, i.e. while the
autouse evaluator patch is live, which is exactly the poisoned-worker
condition. Every ``Depends(get_authz_evaluator)`` captured by the routers must
reference the real function from ``syntara.authz.dependencies``, regardless of
what the module attribute points at right now.

The real function object is imported at module (collection) time, before any
monkeypatch replaces the ``syntara.authz.dependencies`` module attribute, so
this binding is guaranteed to be the real function.
"""

import importlib
from collections.abc import Iterator

from fastapi import APIRouter

import syntara.authz.dependencies
from syntara.authz.dependencies import get_authz_evaluator as real_get_authz_evaluator

ROUTER_MODULES = (
    "syntara.aap.router",
    "syntara.approvals.router",
    "syntara.auth.router",
    "syntara.authz.role_assignment_router",
    "syntara.authz.router",
    "syntara.credentials.router",
    "syntara.workflows.executions_router",
)


def _iter_dependant_calls(dependant: object) -> Iterator[object]:
    call = getattr(dependant, "call", None)
    if call is not None:
        yield call
    for sub in getattr(dependant, "dependencies", None) or []:
        yield from _iter_dependant_calls(sub)


def _captured_dependency_calls() -> set[object]:
    """Collect every callable captured by the routers' Depends trees."""
    captured: set[object] = set()
    for module_name in ROUTER_MODULES:
        module = importlib.import_module(module_name)
        for attr in vars(module).values():
            if not isinstance(attr, APIRouter):
                continue
            for route in attr.routes:
                for depends in getattr(route, "dependencies", None) or []:
                    if callable(depends.dependency):
                        captured.add(depends.dependency)
                dependant = getattr(route, "dependant", None)
                if dependant is not None:
                    captured.update(_iter_dependant_calls(dependant))
    return captured


def test_routers_bind_real_get_authz_evaluator() -> None:
    """Every Depends(get_authz_evaluator) the routers captured is the real function, not a patch."""
    captured = _captured_dependency_calls()
    assert real_get_authz_evaluator in captured, (
        "Router modules were imported while a patched get_authz_evaluator was live, so their "
        "Depends(...) captured the patch instead of the real function. Later tests' "
        "dependency_overrides (keyed on the real function) will never match."
    )
    # Partial-poisoning check: the autouse conftest patch replaces the module attribute
    # for this test, so if any Depends captured that live patch the override keying
    # breaks for those routes alone. Without an active patch the check is vacuous.
    live_getter = syntara.authz.dependencies.get_authz_evaluator
    if live_getter is not real_get_authz_evaluator:
        assert live_getter not in captured, (
            "Router modules captured the currently-live patched get_authz_evaluator "
            f"({live_getter!r}) in a Depends(...), so later tests' dependency_overrides "
            "keyed on the real function will never match for those routes."
        )
