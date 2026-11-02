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

The primary guard is in ``tests/integration/conftest.py``, which imports every
module in ``tests.integration.helpers.router_modules.ROUTER_MODULES`` at
collection time, before any fixture or monkeypatch can run. These tests verify
the outcome of that guard: every ``Depends(get_authz_evaluator)`` captured by
the routers must reference the real function from
``syntara.authz.dependencies``, regardless of what the module attribute points
at right now. In the integration suite, removing the collection-time import
loop or moving it after fixture setup lets earlier lifespan-entering tests
poison the imports and these assertions fail.

The file lives under ``tests/unit/`` because it needs no database: the
integration tree's autouse Postgres and Redis fixtures would make these checks
pay container startup in every targeted run.

The real function object is imported at module (collection) time, before any
monkeypatch replaces the ``syntara.authz.dependencies`` module attribute, so
this binding is guaranteed to be the real function.
"""

import ast
import importlib
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter

from syntara.authz.dependencies import get_authz_evaluator as real_get_authz_evaluator
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


def _references_identifier(tree: ast.AST, name: str) -> bool:
    """Return whether the AST uses ``name`` as an identifier (not in a comment or string)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name | ast.Attribute) and getattr(node, "id", getattr(node, "attr", None)) == name:
            return True
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.name == name:
            return True
        if isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                if alias.asname == name or alias.name.split(".")[-1] == name:
                    return True
    return False


def _modules_referencing_get_authz_evaluator() -> set[str]:
    """Derive the modules that reference get_authz_evaluator from source.

    Parses every Python file in the syntara package on disk instead of trusting
    ROUTER_MODULES, so a module that binds the evaluator without being added to
    the tuple fails loudly here rather than silently reintroducing the worker
    poisoning. Scanning all files (not just ``*router.py``) also catches router
    packages, differently named router modules, and aliased imports. The check
    is identifier-based (AST) rather than a substring match so comments,
    docstrings, and unrelated string literals cannot trigger false positives.
    """
    import syntara

    package_root = Path(syntara.__path__[0])
    found: set[str] = set()
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not _references_identifier(tree, "get_authz_evaluator"):
            continue
        relative = path.relative_to(package_root.parent).with_suffix("")
        found.add(relative.as_posix().replace("/", "."))
    return found


def _modules_binding_get_authz_evaluator() -> set[str]:
    """Derive the modules that bind get_authz_evaluator's bare name in source.

    Separate from ``_modules_referencing_get_authz_evaluator`` so the
    exhaustiveness failure can say which new hits need the per-module patch
    (modules that bind the name) and which only need the early import
    (modules that reach it through the definition module).
    """
    import syntara

    package_root = Path(syntara.__path__[0])
    found: set[str] = set()
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        binds = any(
            (
                isinstance(node, ast.ImportFrom)
                and node.module == "syntara.authz.dependencies"
                and any(alias.name == "get_authz_evaluator" for alias in node.names)
            )
            or (isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "get_authz_evaluator")
            for node in tree.body
        )
        if binds:
            relative = path.relative_to(package_root.parent).with_suffix("")
            found.add(relative.as_posix().replace("/", "."))
    return found


def test_router_modules_tuple_is_exhaustive() -> None:
    """ROUTER_MODULES must list every module that references get_authz_evaluator."""
    derived = _modules_referencing_get_authz_evaluator()
    non_router_hits = derived - set(ROUTER_MODULES)
    binders = non_router_hits & _modules_binding_get_authz_evaluator()
    reference_only = non_router_hits - binders
    assert non_router_hits == {"syntara.authz.dependencies"}, (
        "Modules other than syntara.authz.dependencies (the definition site) now "
        "reference get_authz_evaluator without being listed in ROUTER_MODULES; "
        "update tests/integration/helpers/router_modules.py to match. Only modules "
        "that bind the bare name need the per-module patch; modules that reach the "
        "function through syntara.authz.dependencies only need the early import. "
        f"binding the name: {sorted(binders)}; referencing it only: {sorted(reference_only)}"
    )
    missing = set(ROUTER_MODULES) - derived
    assert not missing, (
        "ROUTER_MODULES lists modules that no longer reference get_authz_evaluator; "
        f"remove them from tests/integration/helpers/router_modules.py: {sorted(missing)}"
    )


def test_references_identifier_ignores_comments_and_strings() -> None:
    """The AST scan must not flag mentions living in comments or string literals."""
    tree = ast.parse(
        "# get_authz_evaluator mentioned in a comment\n"
        '"""Module docstring mentioning get_authz_evaluator."""\n'
        'PLACEHOLDER = "get_authz_evaluator inside a string literal"\n'
    )
    assert not _references_identifier(tree, "get_authz_evaluator")


def test_references_identifier_catches_code_references() -> None:
    """The AST scan must flag imports, aliased imports, attribute access, and definitions."""
    tree = ast.parse(
        "from syntara.authz.dependencies import get_authz_evaluator\n"
        "from syntara.authz.dependencies import get_authz_evaluator as evaluate\n"
        "import syntara.authz.dependencies\n"
        "binding = syntara.authz.dependencies.get_authz_evaluator\n"
        "async def get_authz_evaluator(): ...\n"
    )
    assert _references_identifier(tree, "get_authz_evaluator")
