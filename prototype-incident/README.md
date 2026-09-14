# Incident triage and remediation prototype

**Accurate as of completion of [prototype phase A](../prototype-plan/agentic-orchestration-prototyping-plan.md#prototype-a--agentruntime-boundary-harnesscontrol-plane-split-working-sandbox--q3-q6-q8-q10-q13-partial-q15-minimal)**

This directory contains a runnable incident workflow for Syntara. An incident webhook starts an agent that investigates read-only incident tools and recommends an Ansible Automation Platform (AAP) job template. A human approves or rejects the recommendation, and approval starts a separate deterministic workflow that runs the selected AAP job.

The prototype drives Syntara through its API and includes supporting changes in `backend/`, with corresponding `frontend/` changes likely as the work evolves. It is research code for exercising the current orchestration, policy, agent runtime, and sandbox paths; it is not production-ready enforcement.

## What you will run

The setup creates two published workflows in the `incident-response` project:

1. `incident-triage` receives the `incident-alert` webhook, calls the synthetic incident MCP tools, optionally discovers AAP job templates through AAP's MCP server, and produces a structured recommendation.
2. `incident-remediation` accepts the approved job template name and host limit, launches that job through the AAP integration, and records a summary.

The triage service account can read the investigation tools and call the Syntara execution API after approval. The AAP execution credential is attached only to the remediation workflow. The setup also seeds an identity and policy dataset so policy checks run against a useful volume instead of an empty database.

## Requirements

Run the repository setup from the repository root before using this directory:

```bash
make install
make secrets
make certs
```

You also need Podman, a reachable AAP Gateway, a reachable AAP MCP server, and an LLM provider that exposes the model you configure. The local Syntara API must be available through the full containerized stack; `make dev` and `make services-up` do not provide the network layout required by this workflow.

## Configure

Run the remaining commands from `prototype-incident/`:

```bash
cp .env.example .env
$EDITOR .env
set -a; source .env; set +a
```

Set these required values in `.env`:

- `PROTOTYPE_INCIDENT_AAP_BASE_URL`: the AAP Gateway base URL.
- `PROTOTYPE_INCIDENT_AAP_TOKEN`, or `PROTOTYPE_INCIDENT_AAP_USERNAME` and `PROTOTYPE_INCIDENT_AAP_PASSWORD`: credentials for the AAP Gateway.
- `PROTOTYPE_INCIDENT_AAP_MCP_BASE_URL`: the AAP MCP server URL.
- `PROTOTYPE_INCIDENT_AAP_MCP_TOKEN`: a bearer token for the AAP MCP server.
- `PROTOTYPE_INCIDENT_LLM_API_KEY`: the LLM provider API key.
- `PROTOTYPE_INCIDENT_LLM_MODEL_NAME`: a model id returned by that provider's model catalog.

The `.env.example` file documents optional values, including the AAP TLS settings, seed volume, denied action, internal Syntara URL, and approval user. The prototype uses `https://localhost:8000/api/v1` for host-side API calls and `https://syntara:8000/api/v1` for workflow nodes running inside the compose network. Keep the allowed-host values from `.env.example` unless your compose setup uses different service names.

The seed step writes the service-account client id and one-time client secret to `.env.prototype.local`. That file is gitignored. Do not delete it between setup and verification, and do not expect a later idempotent seed to recover a secret that the API already returned on an earlier run.

## Start and seed

Bring up the full stack and then create the prototype resources:

```bash
make stack-up
make seed
```

`make stack-up` includes the prototype MCP server and applies the two local worker fixes required by the compose setup: it maps `syntara` to the Syntara container for the Temporal workers and adds the development CA to their trust store. Re-run `make stack-recreate` after changing `.env`; container environment is read at startup.

`make seed` starts `incident-mcp-server`, seeds the identity and policy volume, creates the AAP and LLM integrations, creates the service account and permissions, creates the incident job template, and publishes both workflows. The seed operations are safe to repeat and look up resources by name where possible.

Use `make help` to see the individual targets if you need to repeat only `mcp-up`, `seed-volume`, or `seed-scenario`. Use `make stack-down` when you are finished with the full stack.

## Verify the setup

Run the standard checks after seeding:

```bash
make verify
```

This checks the seeded volume, MCP tool discovery, project-scoped AAP credential, default-deny behavior for the configured denied action, the service-account webhook, and the policy-resolution latency baseline.

For the broader runtime check, run:

```bash
make verify-a4
```

This validates the seccomp profile and harness boundary, exercises the gate with an allowed and denied request, runs a real webhook-triggered triage, checks that configured provider secrets do not appear in the harness or execution history, runs callback-free standalone parity, approves the recommendation, and verifies that the remediation workflow launches the expected AAP job. It requires the seeded AAP and LLM dependencies. If a specific external dependency is unavailable, run `uv run --project ../backend python verify_prototype_a.py --skip-standalone` or `--skip-remediation` and treat the skipped check as incomplete evidence rather than a pass.

To run only the static seccomp check:

```bash
uv run --project ../backend python ../backend/containers/agent-sandbox/verify_seccomp.py
```

## Run the scenario manually

The verification target runs the complete path automatically. To watch the approval step yourself, source both environment files from this directory:

```bash
set -a; source .env; source .env.prototype.local; set +a
```

Get a token for the seeded service account and fire the webhook:

```bash
TOKEN=$(curl -sk -X POST "https://localhost:8000/api/v1/auth/token" \
  -d "grant_type=client_credentials&client_id=${PROTOTYPE_INCIDENT_SA_CLIENT_ID}&client_secret=${PROTOTYPE_INCIDENT_SA_CLIENT_SECRET}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -sk -X POST "https://localhost:8000/api/v1/webhooks/incident-alert" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"service":"checkout","severity":"critical"}'
```

Save the returned `execution_id`. Inspect the execution and its activities while the agent investigates and waits for approval:

```bash
curl -sk "https://localhost:8000/api/v1/executions/<execution_id>" -H "Authorization: Bearer $TOKEN"
curl -sk "https://localhost:8000/api/v1/executions/<execution_id>/activities" -H "Authorization: Bearer $TOKEN"
```

The triage activity contains the structured recommendation: `job_template_id`, `job_template_name`, `limit`, and `summary`. The approval prompt shows the job template name and summary so a human can review what will run.

Use an administrator token to list the pending approval and approve or reject it. The default approver is `admin`; use the value of `PROTOTYPE_INCIDENT_APPROVER_USERNAME` if you changed it.

```bash
ADMIN_TOKEN=$(curl -sk -X POST "https://localhost:8000/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"'"$(cat ../backend/.secrets/admin-password)"'"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -sk "https://localhost:8000/api/v1/approvals?status=pending" -H "Authorization: Bearer $ADMIN_TOKEN"
curl -sk -X PATCH "https://localhost:8000/api/v1/approvals/<approval_id>" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status":"approved"}'
```

Approval resumes `incident-triage`, which obtains a Syntara token and starts `incident-remediation` with the agent's job template name and host limit. Rejection, expiration, or a rejected fallback decision ends the triage execution without launching remediation. Check the remediation workflow's executions or the AAP job history to confirm the result.

## Standalone parity

After a webhook run creates the triage invocation, you can run the same agent invocation through the worker executor without starting it from Temporal. Obtain the invocation id from the triage activity, then run:

```bash
uv run --project ../backend python run_standalone.py \
  --invocation-id <invocation_id> \
  --result-out /tmp/triage-standalone.json
```

Use `--compare-to /tmp/triage-webhook.json` to compare the stable remediation fields with a saved webhook result. Run this command from a worker-capable environment with access to the same database, Redis, gate URL, and TLS settings; never run it from inside the agent harness.

## Known limitations

- The currently deployed AAP demo MCP server has been observed to fail tool calls. When AAP job-template discovery fails, the triage prompt uses the seeded `Remediate Checkout Payment Gateway Incident` job template and says that the recommendation is a fallback. The rest of the workflow remains runnable through that fallback.
- The prototype expresses credential separation through workflow and integration configuration, but does not provide the final enforcement guarantees. Later work must not treat this setup as proof that an agent cannot obtain the AAP credential.
- The denied-action check uses default deny because the current API does not support explicit deny-effect policies. The triage service account is never granted the configured action, which should make `/authz/can_i` return `allowed: false`.
- Local API requests and the seed client disable TLS certificate verification because the development API uses a self-signed certificate. This setup is for a local development stack only.

The workflow definitions, agent prompt, synthetic incident data, and seed scale live under `fixtures/`. `seed_scenario.py` is the source of the generated resource relationships, while `Makefile` is the supported entry point for running the prototype.
