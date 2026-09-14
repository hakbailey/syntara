"""Set up the incident-triage reference task via the Syntara API.

Creates: the incident project, the AAP target credential + Gateway integration, the scoped
triage service account + client credential, the MCP tool integration, the LLM provider
integration + model, the deny policy/role/assignment, and the remediation + triage workflows
(loaded from fixtures/workflows/*.yaml, published). Also resolves (does not create) AAP's
own built-in Demo Job Template id, via a direct call to AAP's Controller API — a different
system from Syntara, with its own auth — and writes it to .env.prototype.local for use as the
remediation workflow's manual-trigger input.

Pure API calls — this script makes zero imports from syntara internals.
Idempotent: every entity is looked up by name before creation. The one thing that can't be
made idempotent is the service-account client secret (the API only returns it once) — if a
credential already exists for the SA, this script skips creating a new one and leaves the
secrets file alone. The workflows are republished as version 1 on every run; if you change
a fixture such that the definition differs on a re-run, publishing v1 again may fail since
versions are read-only once created — in that case delete the workflow via the API first.

Prereqs: an admin-reachable Syntara API (see api_client.BASE_URL); the incident-mcp-server compose
service running and reachable (see compose.prototype.yml) with
APP_INTEGRATION_URL_ALLOWED_HOSTS including its host name locally; a reachable AAP Gateway
and LLM provider (see .env.example — both are required, not optional, since this
script actually creates and validates those integrations rather than assuming they exist).

Usage: python seed_scenario.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import yaml

import config
from api_client import ApiClient, find_one, get_or_create, post_idempotent

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SECRETS_FILE = Path(__file__).parent / ".env.prototype.local"


def _read_secrets() -> dict[str, str]:
    """Reads what a prior run already wrote (e.g. the SA client secret, only ever
    returned once by the API) — needed because a re-run's own seed_service_account()
    call returns None/None for an already-existing credential, but the secret from the
    run that originally created it is still sitting in this file."""
    if not SECRETS_FILE.exists():
        return {}
    lines = (
        line.split("=", 1)
        for line in SECRETS_FILE.read_text().splitlines()
        if "=" in line
    )
    return {k: v for k, v in lines}


def seed_project(client: ApiClient) -> str:
    project = get_or_create(
        client,
        list_path="/projects",
        list_params={"name": config.PROJECT_NAME},
        match={"name": config.PROJECT_NAME},
        create_path="/projects",
        create_body={
            "name": config.PROJECT_NAME,
            "description": "Reference task: incident triage & remediation",
        },
        label=f"project {config.PROJECT_NAME}",
    )
    return project["id"]


def _require(value: str | None, env_var: str) -> str:
    if not value:
        sys.exit(f"{env_var} is required — see prototype-incident/.env.example")
    return value


def _get_credential_type_id(client: ApiClient, type_name: str) -> str:
    credential_types = client.get("/credential_types")["resources"]
    credential_type = find_one(credential_types, name=type_name)
    if credential_type is None:
        sys.exit(
            f"CredentialType '{type_name}' not found — is the backend fully seeded?"
        )
    return credential_type["id"]


def seed_aap_credential(client: ApiClient, project_id: str) -> str:
    aap_type_id = _get_credential_type_id(client, config.AAP_CREDENTIAL_TYPE_NAME)

    if config.AAP_OAUTH_TOKEN:
        inputs = {"oauth_token": config.AAP_OAUTH_TOKEN}
    elif config.AAP_USERNAME and config.AAP_PASSWORD:
        inputs = {"username": config.AAP_USERNAME, "password": config.AAP_PASSWORD}
    else:
        sys.exit(
            "PROTOTYPE_INCIDENT_AAP_TOKEN (or PROTOTYPE_INCIDENT_AAP_USERNAME + "
            "_AAP_PASSWORD) is required — see prototype-incident/.env.example"
        )

    existing = find_one(
        client.get(f"/projects/{project_id}/credentials")["resources"],
        name=config.AAP_CREDENTIAL_NAME,
    )
    if existing:
        sys.stdout.write(
            f"  [exists] credential {config.AAP_CREDENTIAL_NAME}: {existing['id']}\n"
        )
        return existing["id"]

    credential = client.post(
        f"/projects/{project_id}/credentials",
        json={
            "name": config.AAP_CREDENTIAL_NAME,
            "credential_type_id": aap_type_id,
            "description": "Reference-task AAP target credential — attached only to the remediation workflow node.",
            "inputs": inputs,
        },
    )
    sys.stdout.write(
        f"  [created] credential {config.AAP_CREDENTIAL_NAME}: {credential['id']}\n"
    )
    return credential["id"]


def seed_aap_integration(client: ApiClient, aap_credential_id: str) -> str:
    base_url = _require(config.AAP_BASE_URL, "PROTOTYPE_INCIDENT_AAP_BASE_URL")

    integration = get_or_create(
        client,
        list_path="/integrations",
        list_params={"name": config.AAP_INTEGRATION_NAME},
        match={"name": config.AAP_INTEGRATION_NAME},
        create_path="/integrations",
        create_body={
            "name": config.AAP_INTEGRATION_NAME,
            "description": "Reference-task AAP Gateway — supplies the remediation job's connection config.",
            "integration_type": "ansible_automation_platform",
            # Required — confirmed live ("ansible_automation_platform integrations
            # require a management credential for discovery and validation"). Reuses
            # the same credential the remediation workflow's node references; there's
            # no separate "discovery-only" credential concept for this integration type.
            "management_credential_id": aap_credential_id,
            "configuration": {
                "integration_type": "ansible_automation_platform",
                "base_url": base_url,
                "allow_http": config.AAP_ALLOW_HTTP,
                "insecure_skip_tls_verify": config.AAP_INSECURE_SKIP_TLS_VERIFY,
            },
        },
        label=f"AAP integration {config.AAP_INTEGRATION_NAME}",
    )
    validation = client.post(f"/integrations/{integration['id']}/validate")
    if not validation.get("success", validation.get("valid")):
        sys.exit(
            f"AAP integration validation failed: {validation}\n"
            f"Check PROTOTYPE_INCIDENT_AAP_BASE_URL/_AAP_TOKEN point at a reachable AAP Gateway."
        )
    return integration["id"]


def _aap_request_kwargs() -> dict[str, object]:
    if config.AAP_OAUTH_TOKEN:
        return {"headers": {"Authorization": f"Bearer {config.AAP_OAUTH_TOKEN}"}}
    return {"auth": (config.AAP_USERNAME, config.AAP_PASSWORD)}


def _find_aap_job_template(base_url: str, name: str) -> dict | None:
    url = f"{base_url.rstrip('/')}/api/controller/v2/job_templates/"
    response = httpx.get(url, params={"name": name}, **_aap_request_kwargs())
    response.raise_for_status()
    results = response.json()["results"]
    return results[0] if results else None


def seed_incident_job_template(base_url: str) -> tuple[int, str]:
    """Ensures a job template named config.INCIDENT_JOB_TEMPLATE_NAME exists in AAP and
    returns (its numeric id, its organization's name).

    Confirmed live: giving the triage agent's MCP discovery tools only a generic, unrelated
    "Demo Job Template" to find made its recommendation meaningless even when discovery
    worked — nothing about that name/description connects it to a checkout/payment-gateway
    incident. So instead of using AAP's built-in demo template directly, this copies it (via
    AAP's own /job_templates/{id}/copy/ API — brings the project/playbook/inventory/
    credentials along automatically) into a new job template with a name/description that
    actually matches the reference task's scenario, then patches in the description (the
    copy endpoint only accepts `name`). Idempotent: if a job template with our target name
    already exists, reuses it as-is rather than copying again.

    The organization name is used by the real remediation workflow (see
    seed_remediation_workflow) — the aap_job_template node's parameters.job_template_id
    field is `type: integer` in the JSON schema with no template-expression accommodation
    (confirmed live: "'${trigger.job_template_id}' is not of type 'integer'"), unlike
    UUID-shaped fields elsewhere which have dedicated template-aware validators. So the
    workflow references the job template by NAME (job_template_name, a plain string field
    the schema doesn't type-check), which the triage agent already supplies via
    response_schema, and organization_name is required alongside it — assumed constant
    across the reference task's job templates, so it's resolved once here rather than being
    a second per-invocation trigger input.

    Calls AAP's own Controller API directly (a different system from Syntara, with its own
    auth), not anything in this repo."""
    existing = _find_aap_job_template(base_url, config.INCIDENT_JOB_TEMPLATE_NAME)
    if existing:
        sys.stdout.write(
            f"  [exists] AAP job template '{config.INCIDENT_JOB_TEMPLATE_NAME}': id={existing['id']}\n"
        )
        return existing["id"], existing["summary_fields"]["organization"]["name"]

    source = _find_aap_job_template(base_url, config.AAP_JOB_TEMPLATE_NAME)
    if not source:
        sys.exit(
            f"AAP job template '{config.AAP_JOB_TEMPLATE_NAME}' not found at {base_url}. Every AAP "
            f"instance ships one by default — if yours doesn't, set "
            f"PROTOTYPE_INCIDENT_AAP_JOB_TEMPLATE_NAME to an existing job template's name to copy from."
        )

    copy_url = (
        f"{base_url.rstrip('/')}/api/controller/v2/job_templates/{source['id']}/copy/"
    )
    response = httpx.post(
        copy_url,
        json={"name": config.INCIDENT_JOB_TEMPLATE_NAME},
        **_aap_request_kwargs(),
    )
    response.raise_for_status()
    created = response.json()

    patch_url = (
        f"{base_url.rstrip('/')}/api/controller/v2/job_templates/{created['id']}/"
    )
    patch_response = httpx.patch(
        patch_url,
        json={"description": config.INCIDENT_JOB_TEMPLATE_DESCRIPTION},
        **_aap_request_kwargs(),
    )
    patch_response.raise_for_status()

    sys.stdout.write(
        f"  [created] AAP job template '{config.INCIDENT_JOB_TEMPLATE_NAME}': id={created['id']} "
        f"(copied from '{config.AAP_JOB_TEMPLATE_NAME}')\n"
    )
    return created["id"], created["summary_fields"]["organization"]["name"]


def seed_service_account(
    client: ApiClient, project_id: str
) -> tuple[str, str | None, str | None]:
    """Returns (service_account_id, client_id, client_secret). client_id/secret are None
    if a credential already existed (the API only returns the plaintext secret once)."""
    sa = get_or_create(
        client,
        list_path="/service_accounts",
        list_params={"project_id": project_id},
        match={"name": config.SERVICE_ACCOUNT_NAME},
        create_path="/service_accounts",
        create_body={
            "name": config.SERVICE_ACCOUNT_NAME,
            "description": "Scoped principal for the incident-alert webhook trigger.",
            "project_id": project_id,
        },
        label=f"service account {config.SERVICE_ACCOUNT_NAME}",
    )
    sa_id = sa["id"]

    existing_creds = client.get(f"/service_accounts/{sa_id}/credentials")["resources"]
    active = [c for c in existing_creds if c.get("status") == "active"]
    if active:
        sys.stdout.write(
            f"  [exists] service account already has an active client credential ({active[0]['id']}); "
            "its secret was only shown once and cannot be recovered here. If verify.py needs a fresh "
            "secret, rotate it manually via POST /service_accounts/{id}/credentials/{cred_id}/rotate.\n"
        )
        return sa_id, None, None

    credential = client.post(
        f"/service_accounts/{sa_id}/credentials",
        json={"credential_type": "client_credentials", "grace_period_seconds": 3600},
    )
    sys.stdout.write(
        f"  [created] client credential {credential['id']} (client_id={credential['identifier']})\n"
    )
    return sa_id, credential["identifier"], credential["client_secret"]


def _seed_mcp_server_integration(
    client: ApiClient,
    *,
    name: str,
    description: str,
    base_url: str,
    management_credential_id: str | None = None,
    allow_http: bool = False,
) -> str:
    """MCP tool providers are plain Integrations (integration_type "mcp_server"), created,
    validated, and refreshed through the same generic /integrations endpoints as the AAP and
    LLM integrations above — NOT the /tool_manager/tool_providers path backend/tools/
    register_mcp_provider.py uses. That path does not exist anywhere in the current backend
    (verified by grepping the whole source tree); it's a stale dev script, like the dead
    APP_AAP_BASE_URL code found earlier this session. Do not copy that script's endpoints.

    allow_http defaults False (HTTPS required, confirmed live: "Endpoint URL scheme must
    be https") — only our own synthetic incident-mcp-server needs it True, since that
    container has no TLS. AAP's real MCP server should have real TLS, so its own call
    site leaves this at the secure default."""
    create_body: dict[str, object] = {
        "name": name,
        "description": description,
        "integration_type": "mcp_server",
        "configuration": {
            "integration_type": "mcp_server",
            "base_url": base_url,
            "allow_http": allow_http,
        },
    }
    if management_credential_id:
        create_body["management_credential_id"] = management_credential_id

    integration = get_or_create(
        client,
        list_path="/integrations",
        list_params={"name": name},
        match={"name": name},
        create_path="/integrations",
        create_body=create_body,
        label=f"MCP integration {name}",
    )
    integration_id = integration["id"]

    validation = client.post(f"/integrations/{integration_id}/validate")
    if not validation.get("success", validation.get("valid")):
        sys.exit(
            f"MCP integration '{name}' validation failed: {validation}\nIs {base_url} reachable?"
        )
    refresh = client.post(f"/integrations/{integration_id}/refresh")
    sys.stdout.write(f"  refreshed tools for {name}: {refresh}\n")
    return integration_id


def seed_mcp_integration(client: ApiClient) -> str:
    return _seed_mcp_server_integration(
        client,
        name=config.MCP_PROVIDER_NAME,
        description="Incident-triage prototype read-only tools",
        base_url=config.MCP_BASE_URL,
        allow_http=True,
    )


AAP_MCP_JOB_TEMPLATE_TOOL_NAMES = frozenset(
    {"job_templates_list", "job_templates_retrieve"}
)


def seed_aap_mcp_integration(
    client: ApiClient, project_id: str
) -> tuple[list[str], str, str]:
    """Registers AAP's own real MCP server (not our synthetic one) so the triage agent can
    discover AAP job templates itself, by name/description, instead of a human needing a
    pre-resolved job_template_id from a side channel. Returns (tool_ids, integration_id,
    credential_id).

    tool_ids is filtered to AAP_MCP_JOB_TEMPLATE_TOOL_NAMES — AAP's MCP server exposes ~60
    tools covering most of its API (users, teams, credentials, activity streams, ...), and
    giving the agent all of them made it unable to find the two relevant ones (confirmed
    live: it gave up, claiming no discovery tool existed). Still read-only in effect either
    way — these are discovery tools only, not a way to launch anything; launching only ever
    happens through the separate, deterministic remediation workflow.

    integration_id/credential_id are needed by the triage node's own integration_connections
    (see triage.yaml) — confirmed live: management_credential_id (set below) authenticates
    integration refresh/health-check calls only; Syntara never uses it during workflow execution,
    so without an explicit execution-time credential the agent's calls to this integration
    are unauthenticated and 401. Reusing the same bearer credential for both is fine here —
    there's no separate "runtime" AAP MCP token to provision."""
    base_url = _require(config.AAP_MCP_BASE_URL, "PROTOTYPE_INCIDENT_AAP_MCP_BASE_URL")
    token = _require(config.AAP_MCP_TOKEN, "PROTOTYPE_INCIDENT_AAP_MCP_TOKEN")
    bearer_type_id = _get_credential_type_id(client, "HTTP Bearer Token")

    credential = get_or_create(
        client,
        list_path=f"/projects/{project_id}/credentials",
        list_params={},
        match={"name": config.AAP_MCP_CREDENTIAL_NAME},
        create_path=f"/projects/{project_id}/credentials",
        create_body={
            "name": config.AAP_MCP_CREDENTIAL_NAME,
            "credential_type_id": bearer_type_id,
            "description": "Bearer token for AAP's own MCP server (job template discovery).",
            "inputs": {"token": token},
        },
        label=f"AAP MCP credential {config.AAP_MCP_CREDENTIAL_NAME}",
    )

    integration_id = _seed_mcp_server_integration(
        client,
        name=config.AAP_MCP_INTEGRATION_NAME,
        description="AAP's own MCP server — job template discovery for the triage agent.",
        base_url=base_url,
        management_credential_id=credential["id"],
    )
    tools = client.get(
        "/tools", params={"integration_id": integration_id, "limit": 100}
    )["resources"]
    if not tools:
        sys.exit(f"AAP MCP server at {base_url} exposed no tools after refresh.")
    job_template_tools = [
        t for t in tools if t["name"] in AAP_MCP_JOB_TEMPLATE_TOOL_NAMES
    ]
    if not job_template_tools:
        sys.exit(
            f"AAP MCP server at {base_url} didn't expose any of {sorted(AAP_MCP_JOB_TEMPLATE_TOOL_NAMES)} "
            f"among its {len(tools)} tool(s) — check the server's tool names haven't changed."
        )
    sys.stdout.write(
        f"  discovered {len(tools)} AAP MCP tool(s), selected {[t['namespaced_name'] for t in job_template_tools]}\n"
    )
    return [t["id"] for t in job_template_tools], integration_id, credential["id"]


def resolve_tool_ids(client: ApiClient, integration_id: str) -> dict[str, str]:
    tools = client.get(
        "/tools", params={"integration_id": integration_id, "limit": 100}
    )["resources"]
    resolved = {}
    for fn_name in config.TOOL_FUNCTION_NAMES:
        match = find_one(tools, namespaced_name=fn_name) or next(
            (t for t in tools if t["namespaced_name"].endswith(fn_name)), None
        )
        if match is None:
            sys.exit(
                f"Tool '{fn_name}' not found after refresh — check incident-mcp-server logs."
            )
        resolved[fn_name] = match["id"]
    return resolved


def seed_llm_integration(client: ApiClient, project_id: str) -> tuple[str, str]:
    """Returns (llm_model_id, credential_id) for the triage agent's node parameters."""
    api_key = _require(config.LLM_API_KEY, "PROTOTYPE_INCIDENT_LLM_API_KEY")
    model_name = _require(config.LLM_MODEL_NAME, "PROTOTYPE_INCIDENT_LLM_MODEL_NAME")
    llm_type_id = _get_credential_type_id(client, config.LLM_CREDENTIAL_TYPE_NAME)

    credential = get_or_create(
        client,
        list_path=f"/projects/{project_id}/credentials",
        list_params={},
        match={"name": config.LLM_CREDENTIAL_NAME},
        create_path=f"/projects/{project_id}/credentials",
        create_body={
            "name": config.LLM_CREDENTIAL_NAME,
            "credential_type_id": llm_type_id,
            "description": "Reference-task LLM provider key for the triage agent.",
            "inputs": {"api_key": api_key},
        },
        label=f"LLM credential {config.LLM_CREDENTIAL_NAME}",
    )
    credential_id = credential["id"]

    integration = get_or_create(
        client,
        list_path="/integrations",
        list_params={"name": config.LLM_INTEGRATION_NAME},
        match={"name": config.LLM_INTEGRATION_NAME},
        create_path="/integrations",
        create_body={
            "name": config.LLM_INTEGRATION_NAME,
            "description": "Reference-task LLM provider — backs the triage agent's model.",
            "integration_type": "llm_provider",
            "management_credential_id": credential_id,
            "configuration": {
                "integration_type": "llm_provider",
                "provider_hint": config.LLM_PROVIDER_HINT,
                "base_url": config.LLM_BASE_URL,
            },
        },
        label=f"LLM integration {config.LLM_INTEGRATION_NAME}",
    )
    integration_id = integration["id"]

    validation = client.post(f"/integrations/{integration_id}/validate")
    if not validation.get("success", validation.get("valid")):
        sys.exit(
            f"LLM integration validation failed: {validation}\n"
            f"Check PROTOTYPE_INCIDENT_LLM_API_KEY / _LLM_BASE_URL point at a reachable provider."
        )

    refresh = client.post(f"/integrations/{integration_id}/refresh")
    sys.stdout.write(f"  refreshed LLM models: {refresh}\n")

    models = client.get(
        f"/integrations/{integration_id}/models", params={"model_id": model_name}
    )["resources"]
    if not models:
        sys.exit(
            f"Model '{model_name}' not found in {config.LLM_INTEGRATION_NAME}'s catalog after refresh — "
            f"check PROTOTYPE_INCIDENT_LLM_MODEL_NAME matches a model id the provider actually returns."
        )
    sys.stdout.write(f"  resolved model {model_name}: {models[0]['id']}\n")
    return models[0]["id"], credential_id


def seed_deny_policy(client: ApiClient, project_id: str, sa_id: str) -> None:
    """Does not create an explicit deny policy — confirmed live against the API that
    `effect: "deny"` is rejected by the current API. The prototype therefore
    uses default-deny rather than an explicit deny statement, and does not bypass
    the API through internal imports.

    The reference task's "denied action" goal (goals doc questions 9/10) is met instead
    by default-deny: confirmed live that a principal with no matching allow gets
    `allowed: false` from `/authz/can_i` (`denied: false` — there's no explicit deny rule,
    just an absent grant). The triage SA is simply never granted
    `PROTOTYPE_INCIDENT_DENY_ACTIONS` (default `credential:read`) by any policy seeded
    here, so it's already denied that action with nothing further to create. This is a
    real, load-bearing finding for the broader effort, not a P0 scripting workaround —
    explicit deny-effect policies aren't available on the current platform at all yet."""
    sys.stdout.write(
        f"  [skip] no explicit deny policy created — deny-effect policies are not supported "
        f"by the API. The triage SA is simply never granted "
        f"'{config.POLICY_DENY_ACTIONS[0]}', so it's denied by default; see this function's "
        f"docstring and prototype-incident/README.md's open items.\n"
    )


def seed_execute_permission(client: ApiClient, project_id: str, sa_id: str) -> None:
    """Grants the triage SA execution:run, scoped to this project — the minimum needed for
    its own trigger_remediation HTTP node to call POST /executions. Deliberately a custom
    policy/role rather than the builtin "project-user" role (which also grants workflow
    CRUD): least-privilege for a non-interactive principal (goals doc question 11), not the
    broadest role that happens to include the permission we need."""
    # Both the policy and the role must be created WITH project_id (not just the
    # statement's scope="project") to be assignable via /projects/{id}/role_assignments —
    # confirmed live in seed_volume.py's role/policy work: a policy/role created without
    # project_id is system-scoped regardless of its statements' scope, and a system-scoped
    # role can't be project-assigned at all ("is a system role and cannot be assigned to a
    # project"). See seed_volume.py's seed_policies/seed_roles docstrings for the full
    # confirmed constraint chain.
    get_or_create(
        client,
        list_path="/policies",
        list_params={"name": config.EXECUTE_POLICY_NAME},
        match={"name": config.EXECUTE_POLICY_NAME},
        create_path="/policies",
        create_body={
            "name": config.EXECUTE_POLICY_NAME,
            "project_id": project_id,
            "description": "Reference-task allow: the triage principal may run executions in this project.",
            "statements": [
                {"effect": "allow", "actions": ["execution:run"], "scope": "project"}
            ],
        },
        label=f"policy {config.EXECUTE_POLICY_NAME}",
    )
    get_or_create(
        client,
        list_path="/roles",
        list_params={"name": config.EXECUTE_ROLE_NAME},
        match={"name": config.EXECUTE_ROLE_NAME},
        create_path="/roles",
        create_body={
            "name": config.EXECUTE_ROLE_NAME,
            "project_id": project_id,
            "description": "Wraps the reference-task execution:run allow policy.",
            "policies": [config.EXECUTE_POLICY_NAME],
        },
        label=f"role {config.EXECUTE_ROLE_NAME}",
    )
    result = post_idempotent(
        client,
        f"/projects/{project_id}/role_assignments",
        {"principal_id": sa_id, "role_name": config.EXECUTE_ROLE_NAME},
    )
    sys.stdout.write(
        f"  [{'created' if result else 'exists'}] execute role assigned to triage service account\n"
    )


def seed_tool_read_permission(client: ApiClient, project_id: str, sa_id: str) -> None:
    """Grant only project-scoped read access to the investigation MCP tools."""
    get_or_create(
        client,
        list_path="/policies",
        list_params={"name": config.TOOL_READ_POLICY_NAME},
        match={"name": config.TOOL_READ_POLICY_NAME},
        create_path="/policies",
        create_body={
            "name": config.TOOL_READ_POLICY_NAME,
            "project_id": project_id,
            "description": "Reference-task allow: the triage principal may read investigation tools.",
            "statements": [
                {"effect": "allow", "actions": ["tool:read"], "scope": "project"}
            ],
        },
        label=f"policy {config.TOOL_READ_POLICY_NAME}",
    )
    get_or_create(
        client,
        list_path="/roles",
        list_params={"name": config.TOOL_READ_ROLE_NAME},
        match={"name": config.TOOL_READ_ROLE_NAME},
        create_path="/roles",
        create_body={
            "name": config.TOOL_READ_ROLE_NAME,
            "project_id": project_id,
            "description": "Wraps the reference-task tool:read allow policy.",
            "policies": [config.TOOL_READ_POLICY_NAME],
        },
        label=f"role {config.TOOL_READ_ROLE_NAME}",
    )
    result = post_idempotent(
        client,
        f"/projects/{project_id}/role_assignments",
        {"principal_id": sa_id, "role_name": config.TOOL_READ_ROLE_NAME},
    )
    sys.stdout.write(
        f"  [{'created' if result else 'exists'}] tool-read role assigned to triage service account\n"
    )


def seed_sa_basic_auth_credential(
    client: ApiClient,
    project_id: str,
    sa_client_id: str | None,
    sa_client_secret: str | None,
) -> str:
    """Wraps the triage SA's own client_id/client_secret in a Syntara "HTTP Basic Auth"
    credential, so the triage workflow's get_token node can auto-inject an Authorization:
    Basic header via credential_id (see http_request_activity.py's credential auth
    injection) rather than the workflow author templating it by hand.

    Can be created either from a freshly-generated sa_client_id/secret (this run) or from
    one a PRIOR run already wrote to .env.prototype.local (the API only ever returns the
    plaintext secret once, but once saved it stays valid and reusable across re-runs) — if
    neither is available, this fails loudly rather than silently skipping a step the
    triage workflow depends on."""
    existing = find_one(
        client.get(f"/projects/{project_id}/credentials")["resources"],
        name=config.SA_BASIC_AUTH_CREDENTIAL_NAME,
    )
    if existing:
        sys.stdout.write(
            f"  [exists] credential {config.SA_BASIC_AUTH_CREDENTIAL_NAME}: {existing['id']}\n"
        )
        return existing["id"]

    if not (sa_client_id and sa_client_secret):
        saved = _read_secrets()
        sa_client_id = saved.get("PROTOTYPE_INCIDENT_SA_CLIENT_ID")
        sa_client_secret = saved.get("PROTOTYPE_INCIDENT_SA_CLIENT_SECRET")
        if sa_client_id and sa_client_secret:
            sys.stdout.write(
                f"  using SA client credential saved by a prior run ({SECRETS_FILE})\n"
            )

    if not (sa_client_id and sa_client_secret):
        sys.exit(
            f"Credential '{config.SA_BASIC_AUTH_CREDENTIAL_NAME}' doesn't exist yet, and no SA client_id/secret "
            f"is available either from this run or from {SECRETS_FILE}. Rotate the SA's client credential via "
            "POST /service_accounts/{id}/credentials/{cred_id}/rotate and re-run."
        )

    bearer_type_id = _get_credential_type_id(client, "HTTP Basic Auth")
    credential = client.post(
        f"/projects/{project_id}/credentials",
        json={
            "name": config.SA_BASIC_AUTH_CREDENTIAL_NAME,
            "credential_type_id": bearer_type_id,
            "description": "Triage SA's own client credentials, for its workflow to re-authenticate as itself.",
            "inputs": {"username": sa_client_id, "password": sa_client_secret},
        },
    )
    sys.stdout.write(
        f"  [created] credential {config.SA_BASIC_AUTH_CREDENTIAL_NAME}: {credential['id']}\n"
    )
    return credential["id"]


def _substitute(text: str, substitutions: dict[str, str]) -> str:
    """Placeholder substitution that re-indents multi-line values to match the
    placeholder's own line indentation, so values dropped into a YAML block scalar
    (e.g. the multi-line triage prompt) don't break the surrounding indentation."""
    for key, value in substitutions.items():
        token = "{{" + key + "}}"
        if "\n" not in value:
            text = text.replace(token, value)
            continue
        lines = []
        for line in text.splitlines(keepends=True):
            if token not in line:
                lines.append(line)
                continue
            indent = line[: len(line) - len(line.lstrip(" "))]
            indented_value = ("\n" + indent).join(value.splitlines())
            lines.append(line.replace(token, indented_value))
        text = "".join(lines)
    return text


def _load_workflow_template(name: str, substitutions: dict[str, str]) -> dict:
    text = (FIXTURES_DIR / "workflows" / name).read_text()
    text = _substitute(text, substitutions)
    return yaml.safe_load(text)


def _create_and_publish_workflow(
    client: ApiClient, name: str, project_id: str, definition: dict
) -> str:
    client.post("/workflows/validate", json={"workflow_definition": definition})
    existing = find_one(
        client.get("/workflows", params={"project_id": project_id})["resources"],
        name=name,
    )
    if existing:
        workflow_id = existing["id"]
        sys.stdout.write(f"  [exists] workflow {name}: {workflow_id}\n")
    else:
        created = client.post(
            "/workflows",
            json={
                "name": name,
                "workflow_definition": definition,
                "project_id": project_id,
                "is_import": False,
            },
        )
        workflow_id = created["id"]
        sys.stdout.write(f"  [created] workflow {name}: {workflow_id}\n")
    # Must target current_version, not a hardcoded "1": publish_workflow_version only creates
    # a new version when workflow_definition differs from the target version. If it doesn't
    # (e.g. re-running seed-scenario with no fixture changes) it falls back to publishing
    # whatever version the URL names — hardcoding "1" there would silently re-publish the
    # very first (possibly long-outdated) version on every unchanged re-run. Confirmed live:
    # this exact bug re-published a stale triage.yaml revision after later runs stopped
    # changing its content.
    current_version = client.get(f"/workflows/{workflow_id}")["current_version"]
    published = client.post(
        f"/workflows/{workflow_id}/versions/{current_version}/publish",
        json={"workflow_definition": definition},
    )
    sys.stdout.write(f"  published {name} v{published['published_version_number']}\n")
    return workflow_id


def seed_remediation_workflow(
    client: ApiClient,
    project_id: str,
    aap_credential_id: str,
    aap_integration_id: str,
    aap_organization_name: str,
    llm_model_id: str,
    llm_credential_id: str,
) -> str:
    definition = _load_workflow_template(
        "remediation.yaml",
        {
            "AAP_CREDENTIAL_ID": aap_credential_id,
            "AAP_INTEGRATION_ID": aap_integration_id,
            "AAP_ORGANIZATION_NAME": aap_organization_name,
            "LLM_MODEL_ID": llm_model_id,
            "LLM_CREDENTIAL_ID": llm_credential_id,
        },
    )
    return _create_and_publish_workflow(
        client, config.REMEDIATION_WORKFLOW_NAME, project_id, definition
    )


def seed_triage_workflow(
    client: ApiClient,
    project_id: str,
    sa_id: str,
    tool_ids: dict[str, str],
    aap_mcp_tool_ids: list[str],
    aap_mcp_integration_id: str,
    aap_mcp_credential_id: str,
    llm_model_id: str,
    llm_credential_id: str,
    sa_basic_auth_credential_id: str,
    remediation_workflow_id: str,
    fallback_job_template_id: int,
    fallback_job_template_name: str,
) -> str:
    aap_mcp_tool_selections = "\n".join(
        f'- "{tool_id}"' for tool_id in aap_mcp_tool_ids
    )
    # Resolved into the prompt text here, not passed through the outer substitutions dict
    # below: _substitute() does a single sequential pass over that dict, so a placeholder
    # embedded *inside* another substituted value (TRIAGE_PROMPT here) would only get
    # replaced if its own dict entry happens to be processed after TRIAGE_PROMPT's — order
    # the dict doesn't guarantee. Resolving it eagerly avoids depending on that ordering.
    triage_prompt = (
        (FIXTURES_DIR / "triage_prompt.md")
        .read_text()
        .rstrip("\n")
        .replace("{{FALLBACK_JOB_TEMPLATE_ID}}", str(fallback_job_template_id))
        .replace("{{FALLBACK_JOB_TEMPLATE_NAME}}", fallback_job_template_name)
    )
    definition = _load_workflow_template(
        "triage.yaml",
        {
            "WEBHOOK_PATH": config.WEBHOOK_PATH,
            "SERVICE_ACCOUNT_ID": sa_id,
            "TRIAGE_PROMPT": triage_prompt,
            "LLM_MODEL_ID": llm_model_id,
            "LLM_CREDENTIAL_ID": llm_credential_id,
            "LOG_TOOL_ID": tool_ids["get_incident_logs"],
            "METRIC_TOOL_ID": tool_ids["get_service_metrics"],
            "AAP_MCP_TOOL_SELECTIONS": aap_mcp_tool_selections,
            "AAP_MCP_INTEGRATION_ID": aap_mcp_integration_id,
            "AAP_MCP_CREDENTIAL_ID": aap_mcp_credential_id,
            "APPROVER_USERNAME": config.APPROVER_USERNAME,
            "APPROVAL_DECISION_WINDOW_SECONDS": config.APPROVAL_DECISION_WINDOW_SECONDS,
            "SYNTARA_INTERNAL_BASE_URL": config.SYNTARA_INTERNAL_BASE_URL.rstrip("/"),
            "SA_BASIC_AUTH_CREDENTIAL_ID": sa_basic_auth_credential_id,
            "REMEDIATION_WORKFLOW_ID": remediation_workflow_id,
        },
    )
    return _create_and_publish_workflow(
        client, config.TRIAGE_WORKFLOW_NAME, project_id, definition
    )


def _write_secrets(updates: dict[str, str]) -> None:
    """Merges into .env.prototype.local rather than overwriting it, so re-running
    seed_scenario.py doesn't clobber a one-time secret (the SA client secret) written by
    an earlier step or an earlier run."""
    existing: dict[str, str] = {}
    if SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                existing[key] = value
    existing.update(updates)
    SECRETS_FILE.write_text(
        "".join(f"{key}={value}\n" for key, value in existing.items())
    )


def main() -> None:
    client = ApiClient()

    sys.stdout.write("1. Project\n")
    project_id = seed_project(client)

    sys.stdout.write("2. AAP target credential + Gateway integration + job template\n")
    aap_credential_id = seed_aap_credential(client, project_id)
    aap_integration_id = seed_aap_integration(client, aap_credential_id)
    aap_job_template_id, aap_organization_name = seed_incident_job_template(
        config.AAP_BASE_URL
    )
    _write_secrets({"PROTOTYPE_INCIDENT_AAP_JOB_TEMPLATE_ID": str(aap_job_template_id)})
    sys.stdout.write(
        f"  organization='{aap_organization_name}' (id written to {SECRETS_FILE}, for verify.py only — "
        f"the remediation workflow's manual trigger takes job_template_name, not the id)\n"
    )

    sys.stdout.write("3. Scoped service account + client credential\n")
    sa_id, client_id, client_secret = seed_service_account(client, project_id)
    if client_id and client_secret:
        _write_secrets(
            {
                "PROTOTYPE_INCIDENT_SA_CLIENT_ID": client_id,
                "PROTOTYPE_INCIDENT_SA_CLIENT_SECRET": client_secret,
            }
        )
        sys.stdout.write(
            f"  wrote client credential to {SECRETS_FILE} (gitignored, one-time secret)\n"
        )

    sys.stdout.write("4. MCP tool integration (synthetic log/metric tools)\n")
    provider_id = seed_mcp_integration(client)
    tool_ids = resolve_tool_ids(client, provider_id)

    sys.stdout.write("5. AAP's own real MCP server (job template discovery)\n")
    aap_mcp_tool_ids, aap_mcp_integration_id, aap_mcp_credential_id = (
        seed_aap_mcp_integration(client, project_id)
    )

    sys.stdout.write("6. LLM provider integration + model\n")
    llm_model_id, llm_credential_id = seed_llm_integration(client, project_id)

    sys.stdout.write(
        "7. Default-deny credential:read + tool:read investigation role + execution:run remediation role\n"
    )
    seed_deny_policy(client, project_id, sa_id)
    seed_tool_read_permission(client, project_id, sa_id)
    seed_execute_permission(client, project_id, sa_id)

    sys.stdout.write(
        "8. SA Basic-Auth credential (for the triage workflow's own get_token node)\n"
    )
    sa_basic_auth_credential_id = seed_sa_basic_auth_credential(
        client, project_id, client_id, client_secret
    )

    sys.stdout.write("9. Remediation workflow\n")
    remediation_workflow_id = seed_remediation_workflow(
        client,
        project_id,
        aap_credential_id,
        aap_integration_id,
        aap_organization_name,
        llm_model_id,
        llm_credential_id,
    )

    sys.stdout.write(
        "10. Triage workflow (publishing syncs the webhook trigger automatically; "
        "approval -> HTTP call closes the loop into the remediation workflow above)\n"
    )
    seed_triage_workflow(
        client,
        project_id,
        sa_id,
        tool_ids,
        aap_mcp_tool_ids,
        aap_mcp_integration_id,
        aap_mcp_credential_id,
        llm_model_id,
        llm_credential_id,
        sa_basic_auth_credential_id,
        remediation_workflow_id,
        aap_job_template_id,
        config.INCIDENT_JOB_TEMPLATE_NAME,
    )

    sys.stdout.write("Scenario seed complete.\n")


if __name__ == "__main__":
    main()
