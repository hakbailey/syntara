"""Reference-task constants and env-driven volume knobs for the incident-triage prototype.

Constants here identify *the* reference task (question 15 in the goals doc) — they are
deliberately not env-driven, because churning them per-run would defeat the point of having
one canonical scenario every later prototype points at. Everything that legitimately varies
(seed volume, which action is denied, secrets) is env-driven instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# --- Reference-task identity (not configurable) ---

PROJECT_NAME = "incident-response"
REMEDIATION_WORKFLOW_NAME = "incident-remediation"
TRIAGE_WORKFLOW_NAME = "incident-triage"
WEBHOOK_PATH = "incident-alert"
SERVICE_ACCOUNT_NAME = "incident-triage-trigger"
MCP_PROVIDER_NAME = "incident-mcp"
AAP_CREDENTIAL_NAME = "incident-remediation-target"
AAP_CREDENTIAL_TYPE_NAME = "Ansible Automation Platform"
AAP_INTEGRATION_NAME = "incident-aap-gateway"
LLM_CREDENTIAL_NAME = "incident-triage-llm-key"
LLM_CREDENTIAL_TYPE_NAME = "LLM Provider"
LLM_INTEGRATION_NAME = "incident-triage-llm-provider"
TOOL_FUNCTION_NAMES = ["get_incident_logs", "get_service_metrics", "list_recent_alerts"]
AAP_MCP_INTEGRATION_NAME = "incident-aap-mcp"
AAP_MCP_CREDENTIAL_NAME = "incident-aap-mcp-token"
SA_BASIC_AUTH_CREDENTIAL_NAME = "incident-triage-sa-basic-auth"
EXECUTE_POLICY_NAME = "incident-triage-execute-policy"
EXECUTE_ROLE_NAME = "incident-triage-execute"
TOOL_READ_POLICY_NAME = "incident-triage-tool-read-policy"
TOOL_READ_ROLE_NAME = "incident-triage-tool-read"

# --- Local network / process config ---

MCP_HOST = os.environ.get("PROTOTYPE_INCIDENT_MCP_HOST", "incident-mcp-server")
MCP_PORT = int(os.environ.get("PROTOTYPE_INCIDENT_MCP_PORT", "8765"))
MCP_BASE_URL = f"http://{MCP_HOST}:{MCP_PORT}/mcp"

# --- What the triage SA is never granted, i.e. denied by default (see
# seed_scenario.py's seed_deny_policy docstring — explicit deny-effect policies aren't
# supported by the API yet) — env-driven so later prototypes can swap it. ---

POLICY_DENY_ACTIONS = os.environ.get(
    "PROTOTYPE_INCIDENT_DENY_ACTIONS", "credential:read"
).split(",")

# --- Live dependencies: required. seed_scenario.py creates the actual AAP integration and
# LLM provider integration/model from these — it fails loudly (not a placeholder) if
# they're missing, since a scenario you can't run end to end isn't the reference task. ---

AAP_BASE_URL = os.environ.get("PROTOTYPE_INCIDENT_AAP_BASE_URL")
AAP_OAUTH_TOKEN = os.environ.get("PROTOTYPE_INCIDENT_AAP_TOKEN")
AAP_USERNAME = os.environ.get("PROTOTYPE_INCIDENT_AAP_USERNAME")
AAP_PASSWORD = os.environ.get("PROTOTYPE_INCIDENT_AAP_PASSWORD")
AAP_ALLOW_HTTP = (
    os.environ.get("PROTOTYPE_INCIDENT_AAP_ALLOW_HTTP", "false").lower() == "true"
)
AAP_INSECURE_SKIP_TLS_VERIFY = (
    os.environ.get("PROTOTYPE_INCIDENT_AAP_INSECURE_SKIP_TLS_VERIFY", "false").lower()
    == "true"
)
# Every AAP instance ships this by default. Not used directly — seed_scenario.py copies it
# (via AAP's own /job_templates/{id}/copy/ API, which brings the project/playbook/inventory/
# credentials along) into a new job template named/described for this reference task, so
# the triage agent's own MCP discovery tools have something to find by name/description
# that actually matches an incident, rather than a generically-named demo template. Needing
# zero pre-existing project/playbook of the reference task's own is still the point — we
# only ever clone AAP's own built-in demo, never require one to already exist.
AAP_JOB_TEMPLATE_NAME = os.environ.get(
    "PROTOTYPE_INCIDENT_AAP_JOB_TEMPLATE_NAME", "Demo Job Template"
)
INCIDENT_JOB_TEMPLATE_NAME = "Remediate Checkout Payment Gateway Incident"
INCIDENT_JOB_TEMPLATE_DESCRIPTION = (
    "Restarts and reconfigures the checkout service's payment gateway integration. Use for "
    "incidents involving PaymentGatewayTimeoutError, elevated checkout error rates, or "
    "payment-related latency spikes."
)
# AAP's own real MCP server (a different thing from AAP's Controller/Gateway REST API above)
# — registered as a second MCP integration so the triage agent can discover job templates
# itself instead of a human needing a pre-resolved id. Required: no default URL is guessed.
AAP_MCP_BASE_URL = os.environ.get("PROTOTYPE_INCIDENT_AAP_MCP_BASE_URL")
AAP_MCP_TOKEN = os.environ.get("PROTOTYPE_INCIDENT_AAP_MCP_TOKEN")

LLM_API_KEY = os.environ.get("PROTOTYPE_INCIDENT_LLM_API_KEY")
LLM_MODEL_NAME = os.environ.get("PROTOTYPE_INCIDENT_LLM_MODEL_NAME")
LLM_BASE_URL = os.environ.get(
    "PROTOTYPE_INCIDENT_LLM_BASE_URL", "https://openrouter.ai/api/v1"
)
LLM_PROVIDER_HINT = os.environ.get("PROTOTYPE_INCIDENT_LLM_PROVIDER_HINT", "custom")

# --- Closing the loop: agent recommends -> human approves -> HTTP call triggers remediation.
# See fixtures/workflows/triage.yaml's approval/get_token/trigger_remediation nodes. ---

# Base URL for the triage workflow's own HTTP nodes to call Syntara's API from inside a running
# workflow (a Temporal worker container's network view) — not necessarily the same host a
# human's browser or the seed scripts use. Defaults to the "syntara" compose service name,
# which only resolves under the FULL containerized stack (`podman-compose up --build`) —
# NOT the hybrid `make dev`/`services-up` mode, where the API runs natively on the host and
# is unreachable from containers (host.containers.internal is hard-blocked as a link-local/
# cloud-metadata address, not just missing from an allowlist). See README.md.
SYNTARA_INTERNAL_BASE_URL = os.environ.get(
    "PROTOTYPE_INCIDENT_SYNTARA_INTERNAL_BASE_URL", "https://syntara:8000/api/v1"
)
APPROVER_USERNAME = os.environ.get("PROTOTYPE_INCIDENT_APPROVER_USERNAME", "admin")
APPROVAL_DECISION_WINDOW_SECONDS = os.environ.get(
    "PROTOTYPE_INCIDENT_APPROVAL_DECISION_WINDOW_SECONDS", "3600"
)


@dataclass(frozen=True)
class IncidentVolume:
    """Env-driven scale knobs for the realistic identity/policy dataset (question 7)."""

    projects: int
    users: int
    service_accounts: int
    custom_roles: int
    custom_policies: int
    role_assignments: int
    groups: int
    seed: int

    @classmethod
    def from_env(cls) -> "IncidentVolume":
        def _int(name: str, default: int) -> int:
            return int(os.environ.get(name, str(default)))

        return cls(
            projects=_int("PROTOTYPE_INCIDENT_PROJECTS", 10),
            users=_int("PROTOTYPE_INCIDENT_USERS", 200),
            service_accounts=_int("PROTOTYPE_INCIDENT_SERVICE_ACCOUNTS", 20),
            custom_roles=_int("PROTOTYPE_INCIDENT_CUSTOM_ROLES", 100),
            custom_policies=_int("PROTOTYPE_INCIDENT_CUSTOM_POLICIES", 100),
            role_assignments=_int("PROTOTYPE_INCIDENT_ROLE_ASSIGNMENTS", 500),
            groups=_int("PROTOTYPE_INCIDENT_GROUPS", 20),
            seed=_int("PROTOTYPE_INCIDENT_SEED", 1337),
        )
