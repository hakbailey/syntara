"""Regression test for router modules binding get_authz_evaluator at import time.

Router modules capture ``get_authz_evaluator`` inside ``Depends(...)`` when the
module is first imported, and the app registers routers during lifespan startup
rather than at import. Whichever test triggers the first lifespan entry in a
pytest-xdist worker decides which monkeypatch is live when the router modules
are first imported. If that import happens while the conftest patch is active,
every router binds the patched getter permanently, and later tests'
``dependency_overrides`` (keyed on the real function object) never match:
credential list visibility then resolves through regopy with no policies and
returns empty lists.

The primary guard is the collection-time import loop in
``tests/integration/conftest.py``. This test verifies the outcome of that
guard where the poisoning can actually happen: this module lives under
``tests/integration`` so the integration conftest has loaded and run its import
loop before the test executes. The AST exhaustiveness checks that keep
``ROUTER_MODULES`` complete live under ``tests/unit`` because they need no
database.
"""

import importlib
from collections.abc import Iterator

from fastapi import APIRouter

from syntara.authz.dependencies import get_authz_evaluator as real_get_authz_evaluator
from tests.integration.conftest import ROUTER_MODULES_IMPORTED_AT_COLLECTION
from tests.integration.helpers.router_modules import ROUTER_MODULES


def _iter_dependant_calls(dependant: object) -> Iterator[object]:
    call = getattr(dependant, "call", None)
    if call is not None:
        yield call
    for sub in getattr(dependant, "dependencies", None) or []:
        yield from _iter_dependant_calls(sub)


def _captured_dependency_calls_by_module() -> dict[str, set[object]]:
    """Collect every callable captured by the routers' Depends trees, per module."""
    captured: dict[str, set[object]] = {}
    for module_name in ROUTER_MODULES:
        module = importlib.import_module(module_name)
        for attr in vars(module).values():
            if not isinstance(attr, APIRouter):
                continue
            for route in attr.routes:
                for depends in getattr(route, "dependencies", None) or []:
                    if callable(depends.dependency):
                        captured.setdefault(module_name, set()).add(depends.dependency)
                dependant = getattr(route, "dependant", None)
                if dependant is not None:
                    captured.setdefault(module_name, set()).update(_iter_dependant_calls(dependant))
    return captured


def test_routers_bind_real_get_authz_evaluator() -> None:
    """Every Depends(get_authz_evaluator) the routers captured is the real function, not a patch."""
    captured_by_module = _captured_dependency_calls_by_module()
    captured = {call for calls in captured_by_module.values() for call in calls}
    assert real_get_authz_evaluator in captured, (
        "Router modules were imported while a patched get_authz_evaluator was live, so their "
        "Depends(...) captured the patch instead of the real function. Later tests' "
        "dependency_overrides (keyed on the real function) will never match."
    )
    # The union above passes when any single module bound the real function.
    # Check every captured getter too, so one module that bound a wrapper (or an
    # older patch) cannot hide behind another module's correct binding.
    wrong_binding = sorted(
        f"{module_name}.{getattr(call, '__qualname__', repr(call))}"
        for module_name, calls in captured_by_module.items()
        for call in calls
        if getattr(call, "__name__", None) == "get_authz_evaluator" and call is not real_get_authz_evaluator
    )
    assert not wrong_binding, (
        "A router module captured a get_authz_evaluator that is not the real function, even "
        f"though another module bound the real one: {wrong_binding}"
    )
    # Partial-poisoning check: the realistic regression is one router module
    # imported later under a live patch (e.g. by a test helper), which leaves
    # the other modules' bindings intact. Any test-defined getter captured in a
    # Depends(...) breaks override keying for those routes alone, so flag every
    # one of them. Checking for test-defined getters (rather than identity with
    # this test's own live patch) is what makes the check effective: the
    # autouse fixture builds a fresh closure per test, so a router poisoned at
    # an earlier import would have captured a different, earlier closure and an
    # identity comparison could never fail.
    poisoned = sorted(
        f"{module_name}.{getattr(call, '__qualname__', repr(call))}"
        for module_name, calls in captured_by_module.items()
        for call in calls
        if getattr(call, "__module__", "").startswith("tests.")
    )
    assert not poisoned, (
        "Router modules captured test-defined getters in Depends(...); later tests' "
        "dependency_overrides keyed on the real function will never match for those "
        f"routes: {poisoned}"
    )


def test_router_modules_were_imported_at_collection_time() -> None:
    """Every router module must already be imported when the integration conftest loads.

    The collection-time loop in tests/integration/conftest.py is the primary
    guard against incident 2: if a router is first imported later, during
    lifespan startup under a live patch, its Depends(...) captures the patch.
    This test reads the snapshot the conftest takes immediately after that
    loop, so removing or moving the loop fails here and names the modules it
    no longer imports at collection time.
    """
    missing = sorted(set(ROUTER_MODULES) - ROUTER_MODULES_IMPORTED_AT_COLLECTION)
    assert not missing, (
        "router modules were not imported at collection time; the import loop in "
        "tests/integration/conftest.py must run before any fixture or monkeypatch: "
        f"{missing}"
    )
