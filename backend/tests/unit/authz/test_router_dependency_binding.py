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
collection time, before any fixture or monkeypatch can run. The outcome check
(every ``Depends(get_authz_evaluator)`` captured by the routers references the
real function) lives in
``tests/integration/api/test_router_dependency_binding.py``, where the
poisoning can actually occur.

This file stays under ``tests/unit/`` because its AST checks need neither a
database nor an import of the application: the integration tree's autouse
Postgres and Redis fixtures would make each run pay container startup. It
keeps ``ROUTER_MODULES`` exhaustive and the identifier scan honest.
"""

import ast
from pathlib import Path

from tests.integration.helpers.router_modules import ROUTER_MODULES


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
