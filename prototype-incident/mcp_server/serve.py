"""Standalone MCP tool server for the incident-triage prototype.

Deliberately independent of orchestrator_test_sdk / backend/test-sdk: stdlib + fastmcp
only, so nothing under backend/ needs to change to serve these tools. Run via
compose.prototype.yml, which reuses the existing mcp-server container image (already has
fastmcp installed) with this file and fixtures/incident_data.json bind-mounted in and the
container command overridden.

Canned response data lives in fixtures/incident_data.json, not here — add a service to the
scenario by editing that file, not this one.

This server only serves the synthetic log/metric tools. AAP-side tools (job template
discovery) come from AAP's own real MCP server, registered as a separate provider in
seed_scenario.py — not reimplemented here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

DATA_PATH = Path(os.environ.get("INCIDENT_DATA_PATH", "/opt/app-root/incident_data.json"))
_DATA: dict[str, Any] = json.loads(DATA_PATH.read_text())

app = FastMCP("Incident Triage Tools")


@app.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


@app.tool()
def get_incident_logs(service: str, since_minutes: int = 60) -> dict[str, Any]:
    """Return recent log lines for a service, most recent first.

    Args:
        service: service name, e.g. "checkout", "payments", "db-primary".
        since_minutes: only return log lines from within this many minutes ago.
    """
    entry = _DATA.get(service)
    if entry is None:
        return {"service": service, "logs": [], "error": f"unknown service '{service}'"}
    logs = [line for line in entry.get("logs", []) if line["ts_minutes_ago"] <= since_minutes]
    return {"service": service, "since_minutes": since_minutes, "logs": logs}


@app.tool()
def get_service_metrics(service: str, metric: str = "error_rate") -> dict[str, Any]:
    """Return a current-vs-baseline value for one metric of a service.

    Args:
        service: service name, e.g. "checkout", "payments", "db-primary".
        metric: metric name, e.g. "error_rate", "latency_p95_ms", "throughput_rps".
    """
    entry = _DATA.get(service)
    if entry is None:
        return {"service": service, "metric": metric, "error": f"unknown service '{service}'"}
    value = entry.get("metrics", {}).get(metric)
    if value is None:
        return {"service": service, "metric": metric, "error": f"unknown metric '{metric}' for '{service}'"}
    return {"service": service, "metric": metric, **value}


@app.tool()
def list_recent_alerts() -> dict[str, Any]:
    """Return the list of currently firing alerts across all services."""
    return {"alerts": _DATA.get("_alerts", [])}


if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8765"))
    app.run(transport="http", host=host, port=port)
