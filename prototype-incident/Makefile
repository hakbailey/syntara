# Convenience targets for the incident-triage reference task. Run from this directory.
#
# Deliberately lives here, not in the root or backend/ Makefile — see README.md's
# "Design" section on why nothing in this prototype touches backend/ or frontend/.
#
# Prereqs this Makefile does NOT handle for you (see README.md "Run"):
#   - source .env (copied from .env.example) — sets APP_INTEGRATION_URL_ALLOWED_HOSTS and
#     every PROTOTYPE_INCIDENT_* var these targets and the main stack need.
#   - The full containerized stack must already be up (`make stack-up`, NOT `make dev`/
#     `services-up` — see README.md's Prereqs for why).
#
# `-f compose.prototype.yml` is included in every stack command below, not just mcp-up/down
# — the full-stack targets also need it for the SSRF allowed-hosts env vars it sets on the
# two Temporal worker services (see that file's header comment). `stack-up`/`stack-recreate`
# additionally run fix-agent-hosts and trust-dev-ca, two live container patches compose can't
# express declaratively — see those targets' own comments for why.

BACKEND_DIR := ../backend
UV_RUN := uv run --project $(BACKEND_DIR)
SECCOMP_PROFILE := $(abspath ../backend/containers/agent-sandbox/seccomp-profile.json)
COMPOSE := AGENT_SANDBOX_SECCOMP_PROFILE=$(SECCOMP_PROFILE) $(UV_RUN) podman-compose -p syntara -f ../podman-compose.yml -f compose.prototype.yml

# `seed`'s prerequisites (mcp-up, seed-volume, seed-scenario) have a real order dependency
# (seed-scenario needs incident-mcp-server already up) that plain prerequisite ordering only
# guarantees under a sequential build. Force that regardless of how make is invoked (e.g.
# `make -j2 seed` would otherwise be free to run them out of order or concurrently).
.NOTPARALLEL:

.PHONY: help stack-up stack-recreate stack-down fix-agent-hosts trust-dev-ca mcp-up mcp-down seed-volume seed-scenario seed verify verify-a4

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

stack-up: ## Bring up the full containerized stack (all main services + this prototype's overrides)
	$(COMPOSE) up --build -d
	@$(MAKE) fix-agent-hosts
	@$(MAKE) trust-dev-ca

stack-recreate: ## Force-recreate the full stack (e.g. after changing .env) — run mcp-down and stack-down first
	$(COMPOSE) up --build --force-recreate -d
	@$(MAKE) fix-agent-hosts
	@$(MAKE) trust-dev-ca

stack-down: ## Tear down the full containerized stack (run mcp-down first if incident-mcp-server is up)
	$(COMPOSE) down

fix-agent-hosts: ## Work around root podman-compose.yml's unconditional 'syntara:host-gateway' extra_hosts entry — runs automatically as part of stack-up, see compose.prototype.yml header
	@SYNTARA_IP=$$(podman inspect syntara_syntara_1 --format '{{.NetworkSettings.Networks.syntara_default.IPAddress}}'); \
	for c in syntara_temporal-worker_1 syntara_temporal-background-worker_1; do \
		podman exec --user root "$$c" sh -c "grep -v '[[:space:]]syntara\$$' /etc/hosts > /tmp/hosts.new && echo '$$SYNTARA_IP syntara' >> /tmp/hosts.new && cat /tmp/hosts.new > /etc/hosts && rm /tmp/hosts.new"; \
	done

trust-dev-ca: ## Append (not replace) the dev TLS CA into both worker containers' certifi bundle — runs automatically as part of stack-up, see compose.prototype.yml header
	@CERTIFI=/opt/app-root/src/.venv/lib64/python3.12/site-packages/certifi/cacert.pem; \
	for c in syntara_temporal-worker_1 syntara_temporal-background-worker_1; do \
		podman exec --user root "$$c" sh -c "grep -q 'Syntara Dev CA (prototype-incident)' $$CERTIFI || (echo '# Syntara Dev CA (prototype-incident)' >> $$CERTIFI && cat /run/secrets/s2s-ca.pem >> $$CERTIFI)"; \
	done

mcp-up: ## Bring up the prototype's own MCP tool server (same network as the main stack)
	$(COMPOSE) up -d incident-mcp-server

mcp-down: ## Tear down the prototype's own MCP tool server
	$(COMPOSE) down incident-mcp-server

seed-volume: ## Seed the realistic identity/policy volume
	$(UV_RUN) python seed_volume.py

seed-scenario: ## Seed the scenario itself (project, credentials, integrations, workflows)
	$(UV_RUN) python seed_scenario.py

seed: mcp-up seed-volume seed-scenario ## Bring up the MCP server and seed all data end to end — the default path, see README.md

verify: ## Run the P0 done-when checks
	$(UV_RUN) python verify.py

verify-a4: ## Run the A4 seccomp, harness-boundary, attack, and gate checks
	$(UV_RUN) python verify_prototype_a.py
