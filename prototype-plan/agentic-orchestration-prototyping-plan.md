# Agentic Orchestration — Prototyping Plan

## Context

Syntara can run AI agents today, but only as a **single built-in agent loop running in-process with no isolation and no policy over what the agent does**. Code-verified: the loop is a LangGraph `StateGraph` built in `agent_orchestrator/services/orchestration_service.py`, driven by `InvocationExecutor` in the FastAPI Syntara service; the Temporal side (`workflow_engine/activities/agentic_activity.py`) HTTP-invokes it and completes async via callback. The agent holds the model key and tool tokens in-process for the run, and **agent tool calls never pass through the OPA/Rego engine** (there is no `tool:execute` action and no `agent` resource type; authz is enforced only at the API boundary via `PermissionChecker`).

That is not the target architecture. We must prove a way to run agents that (a) lets users **bring** pre-built agents and **author** agents in Syntara, both behind a **swappable engine abstraction** (`AgentRuntime`), and (b) makes every agent action **governed, policy-enforced, sandboxed, and auditable** through Syntara's _existing_ stack, exactly as deterministic automation already is. This is a research/architecture effort: the output is **evidence-backed decisions plus throwaway/harvestable prototype code**, not shippable product.

This plan turns the 17 open questions in `prototype-docs/agentic-orchestration-prototyping-goals.md` into a phased set of prototypes, each producing a working artifact with an explicit done-when. Confirmed scope decisions:

- **Reference task:** incident triage & remediation (alert → agent triages → recommends → if policy allows, triggers a _governed remediation workflow_). Chosen because it naturally exercises all six needs in one story: a read-only tool call, a state-change via a governed workflow (invoke-not-modify), a denied out-of-scope action, a secret the agent must never hold, a human-approval point, and a non-interactive (alert) trigger.
- **Cycle scope:** the full effort, phased. All 17 questions, sequenced, with a mandatory single-agent spine first.
- **Behind the `AgentRuntime` boundary we exercise three implementations:** the current **in-process LangGraph loop** (build baseline), **OpenClaw** driven by a thin Syntara control shim (consume harness / build control-plane), and **kagent** (consume control-plane + harness). This gives a genuine three-way engine swap and doubles as the build-vs-consume answer (questions 1, 2).

**Fixed, not up for grabs:** Syntara's shipped OPA/Rego engine and the section-5 invariants (separation of reasoning/execution; one governance plane; containment — agents never hold automation credentials or model keys; workflows-as-governed-actions with invoke≠modify). **Syntara must own the *governance* of agent memory and run-state even where it does not own the *store*.** Ownership of the memory/run-state store is a per-engine finding; the ability to apply policy, audit, principal-scoping, and retention/deletion to that state is a requirement. Everything else is a candidate to evaluate.

---

## Architectural bet (to validate, not assume)

Four layers, with the `AgentRuntime` boundary between what Syntara owns and what it plugs in:

| Layer                                                                                                            | Syntara posture                                | Exercised by                                                                |
| ---------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- | --------------------------------------------------------------------------- |
| Governance & product — policy-per-call, audit, approvals, credentials, identity, authoring, workflows-as-actions | **own** (engine already shipped)               | reuse `authz/`, `audit/`, `approvals/`, `credentials/`, `service_accounts/` |
| Control plane — agent lifecycle, config, routing, tool binding                                                   | **consume** (selection open)                   | kagent vs thin Syntara shim                                                 |
| Harness — agent loop, context, sub-agents                                                                        | **consume, swappable** (build-vs-consume open) | OpenClaw vs in-process loop                                                 |
| Execution substrate — isolation, density                                                                         | **enforce policy now / compose density with 1803** | our own policy-enforcing container this cycle; managed OpenShell/Agent Sandbox stubbed behind the seam |

Two governance planes, enforced in different places:

- **Semantic plane (the gate).** Every declared (MCP) tool call passes through an **Syntara-owned MCP proxy** → Rego policy check + audit + credential injection → forward or refuse. It lives **above** the boundary, so it survives an engine swap and works identically for all three engines (all speak MCP). This sidesteps the worker-process authz-bootstrap problem: tool calls terminate at a FastAPI service where the regopy evaluator is already initialized, not inside the Temporal worker.
- **Structural plane (containment) — a real, policy-enforcing sandbox, not a hand-wave.** The environment **below** the boundary enforces one fixed contract — _the gate is the only way out_ — through three **actually-enforced, actually-attacked** policy classes: (1) **network egress** — deny-all except the Syntara MCP proxy and the model broker; (2) **shell / exec** — the agent cannot spawn an out-of-policy process or reach a raw socket; (3) **filesystem** — a minimal, scoped, mostly-read-only mount with no ambient credentials. Question 8/10 cannot be answered by "we locked the container down"; it is answered only by escape attempts (curl to a non-gate host, raw socket, disallowed shell, credential file read) that are **demonstrably blocked and audited**. The three policy classes are **fixed and must work this cycle**; what stays a hypothesis is only the *mechanism* (our own container substrate with network policy + seccomp/exec restriction + scoped mounts now; a managed hardened substrate later). Harvest `containers/nexus-agent-sandbox/policy.yaml` from the prior prototype as the starting policy shape.

**#1 risk to prove per engine (Prototype B):** per-tool-call governance needs tool calls visible at the interception point, while the swappable boundary only preserves engine-agnostic points. The MCP-proxy chokepoint is the hypothesis that reconciles them. If an engine can take a state-changing action _without_ going through the proxy that the structural plane cannot contain, that engine fails the must-have.

---

## The prototypes

Each is a working artifact with a done-when. `→ qN` marks the goals-doc questions it answers.

### Prototype 0 — Reference task & realistic fixtures _(prerequisite)_

Stand up the incident-triage scenario and, critically, a **realistically-sized policy + identity + DB dataset** (question 7's latency claim is meaningless on a toy — the cost is the DB behind policy resolution). Build:

- Read-only context tools (log/metric lookup) exposed as MCP tools.
- One **governed remediation workflow** (existing deterministic workflow) the agent may _invoke but not modify_.
- One action the reference agent's principal is _denied_.
- One credential the agent must never hold (the remediation target's secret).
- One non-interactive trigger (the alert) wired to the existing webhook/scheduled path.
- Seed data: many policies/roles/assignments/projects so `resolve_effective_policies` + `resolve_user_groups` run at realistic fan-out.

**Done-when:** the scenario's tools, workflow, denied action, credential, and trigger exist and run manually; realistic seed loads. _(gates everything)_

### Prototype A — `AgentRuntime` boundary, harness/control-plane split, working sandbox → q3, q6, q8, q10, q13 (partial), q15 (minimal)

Revised during detailed planning: a standalone-only sandbox (run a throwaway script in a container, leave the real in-workflow path untouched) proves the containment *mechanism* but protects nothing the reference task actually does. Prototype A instead **splits the harness from the control plane**, so the real webhook-triggered reference task is sandboxed, not just a side demo, and both invocation modes share one sandboxed code path instead of two.

- Extract `orchestration_service.py`'s graph-building/execution logic into a shared, dependency-light `agent_loop.py` (no DB/Redis/Temporal) that takes an injected `ToolExecutor`/`LLMClient` and yields structured run events. Extract its Redis-publishing into a reusable `stream_publisher.py`. `OrchestrationService` becomes a thin adapter over both — the **unsandboxed in-process baseline**, kept unchanged in behavior for comparison/fallback.
- New package `agent_orchestrator/runtime/`: an `AgentRuntime` protocol (`execute(agent_definition, invocation_context) -> dict`, same return shape `InvocationExecutor` already expects) plus an explicit **written above/below allocation**: control-plane concerns (persistence, streaming-to-clients, Temporal callback, credentials-at-rest) stay above the boundary; the reasoning loop and tool/model calls sit below it, in the harness. Two implementations: `InProcessRuntime` (wraps `OrchestrationService` unchanged) and `SandboxedRuntime` (new default — calls the harness service over HTTP, republishes its event stream via `stream_publisher`, returns the same result dict). Refactor `InvocationExecutor._init_orchestration`/`_execute_orchestration` to construct the runtime via a factory instead of `OrchestrationService` directly.
- **Build the policy-enforcing sandbox around the harness (not a stub, and not a side demo).** The harness (`agent_loop` + a thin HTTP wrapper, no DB/Redis/Temporal) runs as an always-on hardened container that enforces the three containment policy classes: **network egress** (deny-all except the Syntara gate — now the *only* egress target needed, since both tool calls and model calls route through it), **shell/exec** (no out-of-policy process or raw socket), **filesystem** (scoped, mostly read-only, no ambient credentials). Start from `containers/nexus-agent-sandbox/policy.yaml` as a containment-class checklist only — that prior prototype's containment depends entirely on a running OpenShell gateway with no non-OpenShell mechanism to harvest, so this is built fresh with standard container hardening (cap-drop, seccomp, read-only rootfs). This is the same seam Prototype F later swaps a managed substrate into; here it is real and enforcing, not stubbed. **Per-run isolation/density (one sandbox instance per invocation, warm pooling) is explicitly deferred**, same as the managed-substrate deferral below — this phase proves the containment mechanism holds under attack, not that it's provisioned per run.
- Route tool calls through the gate MVP (MCP proxy): read-only call **allowed + audited**; forbidden call **blocked + audited**; the target secret **injected at the proxy** so the agent never holds it. **Brokering the model call is now load-bearing, not a spike**: because the harness is genuinely isolated for the real reference task (not a throwaway demo), it cannot hold the raw model key, so the gate gains a second endpoint — an LLM completion broker that injects the real key server-side and streams the response back.
- **Attack the sandbox.** A scripted escape suite must be blocked and audited: curl/HTTP to a non-gate host, raw socket open, disallowed shell command, and read of a credential/secret file. A pass is not "the container looked locked down" — it is "each attack was denied and appears in the audit record."

**Done-when:** the real webhook-triggered reference task's triage step executes inside the sandboxed harness (not in-process in `temporal-worker`) with identical behavior to before; a standalone entry point runs the same unchanged agent definition through the same `SandboxedRuntime` with identical output (proving q6 for real, not as a disconnected demo); the gate demonstrations (allow/block, both tool-call and model-call brokering) pass; **every escape attempt in the attack suite is blocked and audited**; the model key and target secret are not present in the harness container's env, filesystem, or Temporal history.

### Prototype B — Engine swap & scorecard → q1, q2, q3, q14

- Add `OpenClawRuntime` (thin Syntara control shim renders config + drives OpenClaw's gateway; terminate A2A at the Syntara edge) and `KagentRuntime` (CRD-based; **all K8s/engine dependency confined to this one class**).
- Swap between all three by **configuration only**; assert **zero Syntara lines change above the boundary** and the written above/below allocation still holds.
- Fill a fixed **scorecard**: native access-control maturity, API/CRD stability, demonstrated interception point, portfolio/licensing fit, both invocation modes, preservation of streaming + token accounting + credential-at-boundary. Run the reference task on each.
- **Agent memory / run-state — separate ownership of the *store* from governance of it.** Determine per engine where memory/run-state actually lives (Syntara-owned+portable vs engine-owned); a store that doesn't survive the swap is a _valid finding_ feeding build-vs-consume, not a failure. **But the store's location is negotiable; governance of it is not**: whoever holds the bytes, Syntara must be able to apply policy over what enters/leaves memory, audit it, scope it to the run's principal, and enforce retention/deletion. Record per engine whether Syntara can govern the state it does not own; **an engine that owns the store but blocks Syntara from governing it fails a must-have**, not merely scores lower.

**Verify-before-building (early spikes):** the pinned kagent version's `spec.backend: openclaw` claim; real kagent CRD schemas (dump, don't assume); kagent's current RBAC status; **A2A maturity** (load-bearing — Syntara's `a2a_client.py` is an uncalled stub, so A2A client+server+signal bridge is greenfield; if it won't stand up, `AgentRuntime` itself is the uniformity seam with A2A as one impl).

**Done-when:** config-only swap across all three engines with no above-boundary code change; scorecard filled; per engine, the memory/run-state *store* location is determined **and** whether Syntara can govern that state (policy, audit, principal-scoping, retention/deletion) is demonstrated. **"No candidate clears every must-have" is a valid, reportable result** — the in-process baseline is the fallback.

### Prototype C — Entry paths: bring & author → q4, q5

- **Bring-your-own:** a pre-built container speaking a standard protocol (A2A) runs under the same boundary/gate/audit without re-authoring. kagent bridges A2A natively; for OpenClaw, terminate A2A at the Syntara edge.
- **Author-in-Syntara:** the no-code declarative tier compiles a Syntara agent definition into engine config — emitting the _same_ definition shape from Prototype A, so one unchanged definition flows through the swap (B) and both modes (E). No external package, no hand-edited engine config. Source-to-image / code-upload tiers deferred.

**Done-when:** an externally-packaged agent and a Syntara-authored agent each run the reference task end to end under the same boundary, gate, and audit as a native run.

### Prototype D — The governance gate over agent actions → q7, q8, q9, q10, q11 _(highest value, highest uncertainty)_

Extend the existing engine — do not stand up a parallel one. Net-new plumbing (code-verified against `authz/`):

- Add `tool:execute` (and an `agent` resource type) to `authz/role_conventions.py`; express per-tool granularity via resource labels/conditions in `authz/rego/authz.rego`.
- Wire the **PEP into the Syntara MCP proxy**: each tool call → `authorize(db, evaluator, AuthzRequest(resource_type="tool", action="execute", ...))`, threading the acting principal. (Proxy runs in a service where the evaluator is initialized, avoiding the worker-process bootstrap gap. If in-workflow gating must also run inside the Temporal worker, stand up regopy + decision cache + a non-request evaluator accessor there — a documented net-new task.)
- **Node permissions:** split add/modify (CRUD-time `PermissionChecker`) from **execute** (runtime); enforce `effective scope = agent scope ∩ workflow declared scope`, invoke≠modify as the default.
- **Policy model:** one **deny** (configurable fail-task/fail-workflow), one **HITL** (new approval-creation path from the gate — reuse `approvals/` + `WorkflowApprovalMixin` for in-workflow; define standalone HITL and the **meaning of "deny" when there is no workflow to fail**), one **allow-with-audit**; register a **policy server as an integration**; record the full decision chain in the audit record.
- **Scoped principal for non-interactive runs:** model the alert-triggered run as a least-privilege, agent/workflow-scoped `ServiceAccount` (project-scoped principal via `service_accounts/`; webhook path already authenticates as an SA) rather than a broad service account; prove a denied action for that principal is blocked + audited.
- **Negative containment:** denied tool call blocked; **workflow-definition edit blocked while invoke is allowed**; both denials in the audit record.
- Build the **Syntara domain MCP server (producer)** — minimum surface `syntara_trigger_workflow` + `syntara_get_credential`. (Syntara is MCP-native as a consumer today via `tool_manager/lib/providers/mcp/mcp_provider.py`; this makes it a producer.)
- **Latency:** budget <100 ms/check including credential injection, plus targets of a 2 second handoff, a 5 second approval notification, and under 500 ms of audit overhead. The real cost is database policy resolution, not policy evaluation alone. **Mitigation: resolve effective policies once per invocation and reuse across the tool loop.** Measure against the realistic seed. Full p95-under-load is **deferred and reported as not-yet-measured**, not lowered.

**Done-when:** against a real engine, all three policy cases run with the decision chain audited; both negative-containment blocks demonstrated; a non-interactive run is governed by a scoped principal with a denied action blocked+audited; representative per-call latency measured on realistic data for both interception points.

### Prototype E — Both invocation modes from one definition → q6

- Run one unchanged agent definition **in-workflow** (via the existing `agentic_activity.py` async-completion seam, now dispatching to `AgentRuntime`) and **standalone**. **Standalone is its own governance host, not a flag:** in-workflow HITL/approval/audit/pre-dispatch-authz hang off `OrchestratorWorkflow` + the Temporal activity boundary; standalone has none of these, so it needs its own gate/approval/audit host.
- **Parity check:** the engine-backed path must preserve deferred credential resolution, token accounting (`TokenUsageRepository`), and Redis/WS streaming — or explicitly record the regression as the cost of consuming.

**Done-when:** both runs pass, both hit the policy check and land an audit record, from one definition.

### Prototype F — Execution-plane composition → q12, q13, q17 _(honest coordination track)_

Composability against the external execution-plane interface is **not provable this cycle** (the interface is not yet available). Deliverables:

- **Credential-timing conflict register.** OpenShell binds credentials **once at sandbox creation, pool-wide**; Syntara binds **per-node at runtime** so the agent never holds a raw secret. Probe in week one against a mock of the documented binding model; classify as resolvable / needs a provider change / architecturally incompatible; mark provisional until the real interface is available.
- **What we build vs what we defer, made explicit.** *Built and enforcing this cycle* (Prototype A): our own policy-enforcing sandbox — network egress, shell/exec, and filesystem policies that hold up under the attack suite. *Deferred behind the same seam*: the **managed** substrate product (OpenShell / Agent Sandbox) with its density, warm-pooling, and cold-start concerns. Prove the seam by **swapping the managed backend's stub in with zero caller-side changes**, and write the **minimal interface** our runtime needs. Do **not** solve cold-start / warm-pooling in this effort. The containment guarantee does not wait on the external substrate; only density/scale does.
- **Trade-off register (q17).** For the recommended direction: scale limit, maturity limit, top unresolved conflict, and the plain statement that _"governed" = authorization-governed, not manipulation-resistant_ (prompt injection / tool-&-MCP poisoning is a named carried risk, not solved here).

**Done-when:** the conflict register exists; the policy-enforcing sandbox from Prototype A runs with the managed backend stubbed behind an unchanged seam; the minimal interface and trade-off register are written. Report *density/managed-substrate* composability as **not yet proven** until we can run against the real interface — while *containment* is already proven by Prototype A's attack suite.

### Prototype G — Reference task end to end → q15

Run the full path — bring **or** author → govern → invoke → sandboxed execute → audit — in a **single run**, not demos stitched on paper: alert fires → scoped-principal agent triages via read-only tools → recommends → policy check → HITL approval → triggers the governed remediation workflow → everything audited.

**Done-when:** the reference task completes end to end in one run through at least one entry path.

### Prototype H — Multi-agent handoff _(stretch)_ → q16

One governed two-agent handoff (e.g. triage agent → remediation agent) through the same boundary + gate, both agents' actions governed and audited. **Lowest priority; a "no" does not block the recommendation.**

---

## Question coverage map

| Question                                    | Prototype                |
| ------------------------------------------- | ------------------------ |
| 1 control-plane selection                   | B                        |
| 2 harness build-vs-consume                  | B                        |
| 3 swap holds + written split                | A, B                     |
| 4 bring external agent                      | C                        |
| 5 author in Syntara                         | C                        |
| 6 both invocation modes                     | A (real in-workflow + standalone, same runtime), E (full parity incl. token accounting/streaming) |
| 7 policy fast enough                        | D                        |
| 8 where the gate sits + non-tool-path contained | A (sandbox attack suite), D |
| 9 policy-server / deny / HITL / allow model | D                        |
| 10 gate + sandbox provably block            | A (attack suite), D      |
| 11 scoped principal for non-interactive     | D                        |
| 12 compose w/ 1803 (credential timing)      | F                        |
| 13 substrate seam: enforce now, defer managed tier | A (enforcing sandbox), F |
| 14 memory/run-state: store location + Syntara governs it | B             |
| 15 reference task end to end                | A (minimal), G (full)    |
| 16 multi-agent                              | H                        |
| 17 trade-offs written                       | F                        |

---

## Sequencing & priority

1. **Non-negotiable:** Prototype 0 → A. (task, data, boundary, gate MVP, containment.)
2. **Serialize the spine:** B's per-engine interception proof → confirm the boundary → **then** build D's full gate. Don't build the gate twice.
3. **Must-haves after the spine:** D (full governance on a real engine) and F (conflict register + containment contract) — they answer the questions that don't depend on a third party cooperating.
4. **Committed but externally risky:** B (three engines) and C (both entry paths). kagent is pre-1.0, mid-rewrite onto Agent Substrate, no RBAC; if it won't stand up, that is a valid question-1 answer and the in-process baseline + OpenClaw carry the swap.
5. **Timebox the three budget-eaters:** kagent bring-up, the author-in-Syntara compiler, the latency harness. On overrun, fall back (drop kagent to a documented finding; reduce authoring to declarative-to-config on one engine; hold latency at single-call on realistic data) rather than starving D and F.

Two **existential decision gates**, either negative being a valid reportable outcome:

- **Interception proof (in B):** does at least one consume candidate expose a chokepoint covering _every_ state-changing tool call? If none → fall back to the in-process baseline (loop above the boundary, consume only the substrate below).
- **Credential timing (in F):** do Syntara's per-node runtime injection+scrub and OpenShell's pool-wide creation-time binding coexist? If architecturally incompatible and 1803 can't change → target a different substrate or bind credentials above the seam and accept the isolation cost.

---

## Key files — reuse & build

**Build (new):**

- `agent_orchestrator/runtime/` — `AgentRuntime` protocol + `InProcessRuntime`, `SandboxedRuntime`, later `OpenClawRuntime`, `KagentRuntime` + factory. K8s dependency confined to `KagentRuntime`.
- `agent_orchestrator/services/agent_loop.py` + `stream_publisher.py` — the graph-execution and Redis-publishing logic extracted out of `orchestration_service.py` so the same loop runs unchanged in both the in-process baseline and the sandboxed harness.
- `agent_orchestrator/harness/` — the sandboxed service (no DB/Redis/Temporal) that `SandboxedRuntime` calls; runs inside the new hardened container.
- Syntara domain **MCP server (producer)** + the **MCP proxy gate** (PEP), plus an **LLM completion broker endpoint** on the same gate (credential-at-boundary for the model key, not just tool credentials). Extend `tool_manager/lib/providers/mcp/mcp_provider.py` (consumer) and `integrations/adapters/mcp_server.py`.
- A2A greenfield: build client + server + A2A→workflow-signal bridge (current `agent_orchestrator/clients/a2a_client.py` is an uncalled stub).
- `authz/role_conventions.py` (+`tool:execute`, `agent` type) and `authz/rego/authz.rego` (per-tool conditions).

**Reuse (do not reinvent):**

- Boundary hang points: `agent_orchestrator/executor/invocation_executor.py` (`_init_orchestration`/`_execute_orchestration`); Temporal seam `workflow_engine/activities/agentic_activity.py` + `workflows/clients/agent_orchestrator_client.py` (async-completion shape).
- Policy: `authz/engine.py::authorize` + `AuthzRequest`; `PermissionChecker` (CRUD-time).
- Credentials-at-boundary: `credentials/lib/injector_resolver.py::InjectorResolver`, `core/services/secret_service.py`, `workflow_engine/activities/credential_resolution_activity.py`, and the `credential_resolver` callback pattern in `invocation_executor.py::_make_mcp_credential_resolver`. Codec/scrubber/interceptor keep secrets out of history/logs.
- Identity/principals: `service_accounts/`, `core/models/principal.py`; webhook SA path in `workflows/webhook_router.py`.
- HITL: `approvals/` + `workflow_engine/approval_mixin.py` (async-completion/signal).
- Audit: `audit/dispatcher.py::AuditEventDispatcher.dispatch` + a new agent tool-call event (`AGENT_INTERACTION` / `LLM_TOOL_CALL` categories exist).
- Token accounting/streaming to preserve across the boundary: `token_manager/repository.py`; `orchestration_service.py`'s Redis stream publishing, extracted into `stream_publisher.py` and reused by `SandboxedRuntime`.
- Prior prototype to harvest **for containment-class shape only, not code**: `automation-nexus/nexus-combined@feature/agentic-orchestration-prototype`. Confirmed this session it has no per-tool-call authorization/audit at all (fully public `/api/v1/agents` endpoints, containment delegated entirely to OpenShell) and its only working sandbox (`OpenShellAgentService`) requires a live OpenShell gateway via gRPC with no non-OpenShell fallback — so `AgentExecutionPlane`/`RuntimeEnvironment` are reference reading only, and `containers/nexus-agent-sandbox/policy.yaml` + `containers/nexus-orchestrator-agent/sandbox-policy.yaml` are used only as a checklist of the three containment classes for the fresh, self-built sandbox in Prototype A.

---

## Verification

- **Boundary/swap (A, B):** run the reference task, then flip an engine-selection config value and re-run with **no code change above `agent_orchestrator/runtime/`**; diff to confirm zero above-boundary edits. Confirm the written above/below allocation still holds and record memory ownership per engine.
- **Gate blocks (A, D):** scripted runs that (1) call a denied tool, (2) attempt a workflow-definition edit — assert each is blocked and lands in the audit record (`audit_events`). Assert the target secret and model key never appear in the container, Temporal history, or logs.
- **Sandbox attack suite (A):** from inside the running sandbox, attempt (1) HTTP/curl to a non-gate host, (2) a raw socket open, (3) a disallowed shell/exec, (4) a read of a credential/secret file — assert each is **denied by the enforced policy** (network egress / shell-exec / filesystem) and audited. This is the proof for question 8's "non-tool-path action is contained" and question 10.
- **Real-path parity (A):** run the actual webhook-triggered reference task end to end and confirm the triage step now executes inside the sandboxed harness, not in-process in `temporal-worker`, with identical output; then run the same unchanged agent definition via the standalone entry point through the same `SandboxedRuntime` and confirm identical output — this is what makes A's sandbox proof real rather than a disconnected demo, and it's the meaningful version of question 6 for this phase (full in-workflow/standalone parity, incl. token accounting and streaming, is still Prototype E's job).
- **Policy model (D):** run one deny (both fail-task and fail-workflow configs), one HITL (approval signal resumes/denies), one allow-with-audit; assert the decision chain is in the execution record. Run a non-interactive (alert) trigger under a scoped SA and confirm a denied action is blocked+audited.
- **Latency (D):** harness measuring per-call authz incl. credential injection against the realistic seed for both interception points; report numbers and mark p95-under-load not-yet-measured.
- **Entry paths (C) & modes (E):** run the reference task via bring and via author; run one definition in-workflow and standalone; assert both hit policy + audit.
- **Compose (F):** the credential-timing experiment output and validation request to the external execution-plane maintainers; swap the substrate stub behind the seam with zero caller changes.
- **End to end (G):** single-run trace from alert to audit.
- Standard gates: `make -C backend format lint test-all typecheck` on all new backend code.

---

## Out of scope this cycle

Production hardening; custom policy-authoring UI; external secrets managers; LLM cost management at scale; failure/rollback/compensation of agent-triggered workflows; the **managed** substrate product — density, warm-pooling, and cold-start, stubbed behind the seam; proving the agent's _reasoning_ is trustworthy — prompt injection and tool/MCP poisoning are named carried risks. **In scope and must work:** a real policy-enforcing sandbox (network egress, shell/exec, filesystem) proven by an attack suite — this is not deferred. We prove the **authorization + containment** gate works against a well-behaved agent; what we do not attempt is defeating an adversarial one. **Also deferred, for the same reason as the managed substrate's density/pooling:** per-invocation sandbox isolation. Prototype A's own hand-rolled harness runs as a single always-on hardened service, not one instance spawned per run — the containment mechanism is proven under attack, but density/pooling for our own sandbox is out of scope this cycle just as it is for the managed tier.

## Open cross-team decisions to surface early

- **rossoctl / Kagenti posture** (consume / contribute / coordinate / diverge) — external ecosystem projects are building overlapping primitives (SPIFFE/mTLS, transparent policy intercept, mission-scoped identity) that could change the build-vs-consume answer for the gate itself. Track these developments as part of the technology evaluation.
- Which **deployment tiers** we support (Syntara-managed engine vs customer harness on the Syntara plane vs customer plane) — governs how much of the guarantee is structural vs asserted, and must be reflected in **coverage reporting** (audit states what was governed and disclaims what it cannot see).
- Whether the agent execution path **requires Kubernetes** (every consume option except in-process does) — smaller than it sounds given the existing `syntara-operator`, but a conscious choice.
