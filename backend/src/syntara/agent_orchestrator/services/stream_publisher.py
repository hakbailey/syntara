"""Redis publisher for events crossing the AgentRuntime boundary."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from syntara.agent_orchestrator.models.streaming_events import CompletionEventData
from syntara.core.cache.stream import StreamClient


class StreamPublisher:
    """Publish the stable invocation event envelope to Redis."""

    async def publish(
        self,
        client: StreamClient,
        stream_id: str,
        event_type: str,
        invocation_id: UUID,
        data: dict[str, Any],
    ) -> None:
        """Publish one already-serializable event payload."""
        await client.publish(
            stream_id,
            {
                "event_type": event_type,
                "invocation_id": str(invocation_id),
                "timestamp": datetime.now(UTC).isoformat(),
                "data": data,
            },
        )

    async def publish_completion(self, client: StreamClient, stream_id: str, invocation_id: UUID) -> None:
        """Publish the terminal successful-completion event."""
        await self.publish(client, stream_id, "completion", invocation_id, CompletionEventData().model_dump())


stream_publisher = StreamPublisher()
