"""Tests for agentic runtime precedence at workflow dispatch."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from syntara.workflows.workflow_engine.dynamic_workflow import OrchestratorWorkflow
from syntara.workflows.workflow_engine.graph import ActivityNode
from syntara.workflows.workflow_engine.models.workflow_definition import NodeType


def _make_workflow(workflow_runtime_engine: str | None) -> OrchestratorWorkflow:
    """Build the dispatch state needed by an agentic node without Temporal startup."""
    workflow = OrchestratorWorkflow.__new__(OrchestratorWorkflow)
    workflow.execution_id = "execution-1"
    workflow.request_id = "request-1"
    workflow._project_id = "project-1"
    workflow._created_by_user_id = "user-1"
    workflow._runtime_service_account_id = "service-account-1"
    workflow._runtime_engine = workflow_runtime_engine
    return workflow


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("node_runtime_engine", "workflow_runtime_engine", "expected_runtime_engine"),
    [
        ("in_process", "sandboxed", "in_process"),
        ("sandboxed", "in_process", "sandboxed"),
        (None, "in_process", "in_process"),
        (None, "sandboxed", "sandboxed"),
        (None, None, None),
    ],
)
async def test_agentic_dispatch_resolves_node_then_workflow_runtime(
    node_runtime_engine: str | None,
    workflow_runtime_engine: str | None,
    expected_runtime_engine: str | None,
) -> None:
    """Node selection wins, workflow selection is the fallback, and unset stays unset."""
    workflow = _make_workflow(workflow_runtime_engine)
    execute_executor_node = AsyncMock(return_value={"output": {"status": "started"}})
    node = ActivityNode(
        node_id="agent-1",
        node_type=NodeType.AGENTIC,
        parameters={},
    )
    parameters = {"runtime_engine": node_runtime_engine} if node_runtime_engine else {}

    with patch.object(workflow, "_execute_executor_node", new=execute_executor_node):
        await workflow._dispatch_node_to_executor(
            node,
            parameters,
            MagicMock(),
            timeout_seconds=300,
        )

    assert execute_executor_node.call_args is not None
    assert execute_executor_node.call_args.kwargs["extra_args"] == [
        "execution-1",
        "request-1",
        "project-1",
        "user-1",
        "service-account-1",
        expected_runtime_engine,
    ]
