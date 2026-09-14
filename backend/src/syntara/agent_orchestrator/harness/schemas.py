"""Wire models shared by the control plane and sandboxed harness."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class HarnessToolDefinition(BaseModel):
    """Serializable tool catalog entry whose execution goes through the gate."""

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    integration_id: UUID
    credential_id: str | None = None
    resource_type: str = "execution"
    action: str = "run"
    resource_project: str = ""


class HarnessInvocationRequest(BaseModel):
    """One prepared agent invocation sent below the runtime boundary."""

    prompt: str
    original_prompt: str
    system_prompt: str
    session_id: str
    invocation_id: UUID
    execution_id: UUID | None = None
    request_id: UUID | None = None
    response_schema: dict[str, Any] | None = None
    context_package: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_name: str
    llm_integration_id: UUID
    llm_credential_id: str
    tools: list[HarnessToolDefinition] = Field(default_factory=list)


class HarnessWireEvent(BaseModel):
    """One NDJSON record emitted by the harness."""

    type: str
    data: dict[str, Any]
