"""Pure tests for standalone runtime-selection controls."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from run_standalone import _apply_runtime_selection  # noqa: E402


def test_explicit_runtime_override_replaces_source_context() -> None:
    context_data: dict[str, object] = {"runtime_engine": "sandboxed"}

    _apply_runtime_selection(
        context_data,
        runtime_engine="in_process",
        deployment_default=False,
    )

    assert context_data["runtime_engine"] == "in_process"


def test_deployment_default_removes_source_context() -> None:
    context_data: dict[str, object] = {"runtime_engine": "sandboxed"}

    _apply_runtime_selection(
        context_data,
        runtime_engine=None,
        deployment_default=True,
    )

    assert "runtime_engine" not in context_data


def test_source_context_is_preserved_without_a_selection_flag() -> None:
    context_data: dict[str, object] = {"runtime_engine": "sandboxed"}

    _apply_runtime_selection(
        context_data,
        runtime_engine=None,
        deployment_default=False,
    )

    assert context_data["runtime_engine"] == "sandboxed"
