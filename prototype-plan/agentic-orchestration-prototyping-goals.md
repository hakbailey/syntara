# Agentic Orchestration: Goals of the Prototyping Effort

---

## 1. Why we are doing this

Syntara can already run AI agents within workflows today, but only as a single built-in agent loop, running in-process, with no isolation and no policy over what the agent does. That is not the architecture we need. What we have to prove is a way to run agents that lets users both bring their own pre-built agents and author agents inside Syntara, all behind a swappable engine abstraction. That same way of running agents must make every agent action governed, policy-enforced, sandboxed, and auditable, exactly as deterministic automation already is. The agents must run as part of IT automation and orchestration work, both as a step inside a Syntara workflow and on their own alongside those workflows.

We want this proof before we commit engineering to a production design.

Several big questions are still open: what we build versus what we consume, where the agent process runs, which control plane and harness to use, how the design fits into the shared execution plane, and how it reuses our existing policy and governance tooling. The prototypes exist to turn those open questions into evidence-backed decisions at low cost, and to give us working proof that the recommended design runs end to end without breaking.

This is a research and architecture effort, not a product delivery. The output is decisions, proof that the risky interfaces actually work, and prototype code we will either throw away or reuse in parts. It is not shippable code.

**A few terms used throughout.** We describe the stack as four layers:

- **Governance and product layer**: what Syntara owns. Policy, approvals, audit, credentials, identity, authoring, and workflows.
- **Control plane**: the component that manages agents (their lifecycle, configuration, and routing). kagent is one candidate.
- **Harness**: the component that runs a single agent's loop (its context, tools, and sub-agents). OpenClaw is one candidate.
- **Execution substrate**: the layer that isolates and sandboxes a running agent (a plain container, or something heavier like OpenShell). The execution plane lives here.

The **`AgentRuntime` boundary** is the interface between the parts Syntara owns and the stack it consumes. "Above the boundary" means code Syntara owns; "below the boundary" means whatever we plug in rather than build. The control plane, harness, and substrate are the candidates to sit below it, but exactly which pieces we consume versus build, and therefore where the line falls, is itself one of the open questions (questions 2, 3). We use **engine** as shorthand for whatever ends up below the boundary.

---

## 2. What is not decided yet

**No tooling or architecture decisions have been made.** Nothing is chosen or mandated: not the control plane, the harness, the substrate, the process boundary, nor even whether we build or consume each piece. kagent, OpenClaw, OpenShell, Agent Sandbox, rossoctl/Kagenti, and BeeAI are examples of candidates the prototypes exist to evaluate; the list is a starting point, not exhaustive, and others may surface as the effort proceeds. They are included as evaluation candidates, not as selected components. Choosing the tooling and architecture is itself a primary output of this effort.

The one exception is Syntara's existing OPA/Rego policy engine. That is a foundation we extend, not a candidate we choose.

---

## 3. What success looks like

The effort succeeds if, at the end, we can state with working evidence, not argument, that each of the following is true. These are the headline outcomes; each maps to one or more questions in section 4, which carry the detailed pass conditions.

1. **We selected a control plane and a harness, and can defend the choice** against a fixed scorecard, with the top candidate clearing every must-have (questions 1, 2).

2. **The abstraction survives a swap**: changing the engine behind the `AgentRuntime` boundary is a configuration change, not a rewrite; where the agent memory and run-state store lives relative to the boundary is determined and demonstrated; and Syntara can govern that memory and run-state (policy, audit, principal-scoping, retention) regardless of which side of the boundary the store sits on (questions 3, 14).

3. **A user can bring a pre-built, externally packaged agent and run it under Syntara's governance.** An agent developed and packaged outside Syntara is brought in and run through the same abstraction, governance, and audit as one Syntara runs natively, without being rebuilt or re-authored inside the system (question 4).

4. **A user can author an agent inside Syntara.** An operator can define, configure, and govern an agent through Syntara's own authoring surface, starting with the no-code declarative tier, producing a runnable agent without supplying an externally-built package or hand-editing engine config (question 5).

5. **One unchanged agent definition runs in both invocation modes**, inside a workflow and standalone, with governance and audit engaged in both (question 6).

6. **Agents run under Syntara's existing governance, not a new parallel one.** The same policy engine, approvals, audit, credentials, and identity that govern deterministic automation govern agent actions too, extended to cover:
   - per-tool-call decisions and agent-node permissions,
   - policy over the MCP servers and integrations agents use,
   - credential injection at the boundary, so the agent never holds a raw secret,
   - a scoped principal for non-interactive runs,

   all within the performance budget (questions 7, 8, 9, 11).

7. **Governance provably blocks, not just permits.** We can demonstrate the enforcement side of the invariants, not just the happy path: a denied tool call and an attempt to modify a workflow definition are both blocked and audited (question 10).

8. **The reference task runs end to end** through the whole path, bring or author, govern, invoke, sandboxed execute, audit, in a single run rather than separate demos stitched together on paper (question 15).

9. **The design composes with the shared execution plane.** Full success means this is proven against its real interface, not a mock we build ourselves. Because that interface is not available yet, the honest milestone this cycle is a shared conflict register (question 12), plus a definition of the minimal interface our runtime needs (question 13). Until we can run against the real interface, we report composability as not yet proven, and we do not lower the bar to pretend otherwise.

10. **More than one agent can be orchestrated together (lowest priority).** At least one multi-agent pattern, one agent handing off to or invoking another, runs under the same abstraction and governance. This is in scope but the lowest-priority outcome; the single-agent path takes precedence (question 16).

Each criterion is met by a working artifact, not an argument. If we can show 1 through 10 and, for the recommended direction, name its trade-offs (its scale limit, its maturity limit, and its top unresolved conflict, question 17), the strategy is proven and a direction is ready to graduate into a productization plan.

---

## 4. The questions the prototypes must answer

These are the decisions the effort exists to resolve. Each is a single question with a pass condition, so we can say yes or no afterward. Every prototype should map to one or more. Several questions reference a single "reference task", one pre-chosen IT-automation task used consistently across prototypes so results are comparable; that task is still to be decided.

1. **Control plane selection.** Against a fixed scorecard (native access control maturity, API stability, where governance can hook in, portfolio and licensing fit, support for both invocation modes, and whether it preserves current capabilities such as streaming, token accounting, and credential-at-the-boundary), which candidate control plane clears every must-have and scores highest when the same reference task runs on at least two candidates? The two-candidate comparison may pit the best consumable option against a build baseline, not necessarily two consumables; this doubles as the build-versus-consume answer in question 2. If no candidate clears every must-have, that finding, that we must build or re-scope, is itself a valid result.

2. **Harness selection, and build versus consume.** Is there a harness we can consume that meets our needs under the chosen control plane, or must we build one? The decision is recorded against the same scorecard.

3. **Does the abstraction hold under a swap, and is the above/below split written down?** Can we swap the engine behind the `AgentRuntime` boundary with configuration-only changes, and is there an explicit written allocation of what lives above versus below the boundary (memory, context, tools, policy)? Answered when a swap changes zero lines of Syntara code above the boundary and that written allocation still holds after the swap.

4. **Can a user bring an externally built agent and run it governed?** Can a user register and run an agent that was developed and packaged outside Syntara (for example a pre-built, containerized agent that speaks a standard agent protocol), and drive it to a real IT outcome, without rebuilding or re-authoring it inside Syntara? Answered when an externally packaged agent runs end to end under the same abstraction, governance gate, and audit as a natively-run agent, brought in as a package rather than reimplemented in Syntara.

5. **Can a user author an agent inside Syntara?** Can an operator define, configure, and govern an agent through Syntara's own authoring surface, starting with the no-code declarative tier, and run it to a real IT outcome, without supplying an externally-built package or hand-editing the underlying engine's configuration? Answered when a Syntara-authored agent runs end to end under the same abstraction, governance gate, and audit as a natively-run agent, with no external package and no engine-specific config written by hand.

6. **Do both invocation modes work from one definition?** Does one unchanged agent definition run both inside a deterministic workflow and standalone alongside workflows, through one abstraction, one governance gate, and one sandbox model, with governance and audit engaged in both? Answered when both runs pass and both hit the policy check and audit record.

7. **Can existing policy govern agent actions fast enough?** Can the existing OPA/Rego engine intercept and decide both individual agent tool calls and agent-node permissions within the target budget: under 100 ms including policy injection, with a 2 second handoff, a 5 second approval notification, and under 500 ms of audit overhead? Answered when representative per-call latency is under 100 ms for both interception points, measured against a realistically-sized policy, identity, and database dataset (not a toy setup, since the likely cost is the database lookup behind policy resolution), and the governance targets hold. A full p95-under-load measurement is deferred this cycle; we report it as not yet measured rather than lowering the bar. This latency is measured once question 8 establishes where the gate sits.

8. **Where can the governance gate sit once the loop is inside the consumed engine?** The agent loop runs inside an engine we consume, which may have no access control of its own. At what point can Syntara's governance gate actually intercept each agent action, and does that point cover every action that changes system state? Answered on two counts: a state-changing tool call is provably blocked from inside that engine's execution path, and a state-changing action taken outside the tool-call path (a shell command or a direct network call) is provably contained, so that every state-changing action is forced through the gate rather than the gate covering only the actions the agent volunteers through it.

9. **What is the governance model for policy servers and for deny and approval behavior?** Can we express and run, end to end, at least one deny case (with configurable fail-task or fail-workflow behavior), one human-approval (HITL) case, and one allow-with-audit case, treating a policy server as a registered integration, with each request, response, and decision recorded in the execution record? Answered when all three run against the real engine and the decision chain appears in the audit record, and when we have defined what a deny means in standalone mode, where there is no workflow to fail.

10. **Does the governance gate provably block, not just permit?** Can we demonstrate the enforcement side of the invariants: an agent's attempt to call a tool its principal is denied is blocked, and an agent's attempt to modify a workflow definition (as opposed to invoking it) is blocked, with both denials landing in the audit record? Answered when both blocks are demonstrated end to end against the real engine, not asserted.

11. **What principal governs non-interactive runs?** Scheduled, webhook, and event-triggered runs have no human initiator. Can the effort define and enforce a least-privilege, agent- or workflow-scoped principal for these runs, rather than falling back to a broadly-privileged service account? Answered when a non-interactive run is governed by a scoped principal and a denied action for that principal is blocked and audited.

12. **Does the design compose with the shared execution plane, starting with credential-at-the-boundary?** The external interface is not available yet, so we cannot run against it this cycle. The first known conflict is credential timing: OpenShell, the candidate substrate, binds credentials when it creates a sandbox, once, for the whole pool, while Syntara binds credentials per node at runtime so the agent never holds a raw secret or model API key directly. Do these two models work together, or do they conflict? Answered by a shared conflict register and a definition of the minimal interface required by the runtime.

13. **Can one substrate seam enforce containment now and defer the managed tier?** Can the same substrate interface run the agent this cycle in a container that actually enforces the containment policies (network egress, shell/exec, and filesystem access), and accept a managed OpenShell or Agent Sandbox backend later with no changes above the seam? The policy-enforcing sandbox is in scope and must work this cycle; only the managed substrate product (density, warm-pooling, cold-start) is deferred to 1803. Answered when the enforcing-container path works and passes the containment attack suite (question 10), and the managed backend can be stubbed behind the same interface with zero caller-side changes.

14. **Who owns the agent memory and run-state store, and can Syntara govern it regardless?** Does the agent memory and run-state store live above the boundary (Syntara owns it, portable across engines) or below it (the engine owns it)? Does that hold in both invocation modes? The location of the store is a finding; the governance of it is a requirement. Independently of where the bytes live, Syntara must own the governance of that memory and run-state: applying policy to what enters and leaves it, auditing it, scoping it to the run's principal, and enforcing retention and deletion. Answered when, for each real engine, the store's location is determined and demonstrated, whatever it turns out to be, the written above/below allocation reflects it, and Syntara's ability to govern that state is demonstrated. If an engine owns the store so it does not survive the swap in question 3, that is a valid finding (the store is not portable config-only) that feeds the build-versus-consume decision, not a failure. But an engine that owns the store and prevents Syntara from governing it fails this criterion rather than being a neutral finding.

15. **Does the reference task run end to end?** Does the reference task run the full path from entry (bring or author) to audit in one run, exercising at least one entry path end to end? Answered when it does.

16. **Can multiple agents be orchestrated together? (lower priority)** Can at least one multi-agent pattern, agent-to-agent handoff or one agent invoking another as a governed action, run through the same abstraction and governance gate as a single agent? Answered when a two-agent handoff completes with both agents' actions governed and audited. This is the lowest-priority question; a no here does not block the recommendation.

17. **Are the trade-offs written down?** For the recommended direction, is there a written register naming its scale limit, its maturity limit, and its top unresolved conflict? Answered when that register exists.

---

## 5. Principles, invariants, and non-goals

This section holds three things: our design principles (each stating what it rules out), the invariants the architecture must hold whatever we choose, and the remaining non-goals. In the principles, honor the stance and stay off the out-of-scope side.

1. **Abstraction over process boundary.** Be explicit about the tools we use to define, manage, and run agents, but do not commit to where or how they run. The boundary is not deferred to dodge the question: it must be pluggable, and it must support both invocation modes (question 6). Out of scope: committing to a single deployment topology.
2. **Prefer consuming over building.** Bias toward reusing suitable existing components rather than building from scratch. Which components to use, and whether a consumable option truly fits, is exactly what the prototypes evaluate. Out of scope: building an agent harness or framework from scratch as a first choice.
3. **Outcome and UX first.** Anchor every prototype to a real IT-automation task, not an infrastructure showcase.
4. **Compose, do not rebuild — but enforce containment now.** Prove the design composes with the shared execution plane; do not re-implement it, and do not solve sandbox cold-start or warm-pooling. The managed execution plane and cold-start problem are outside this effort. What is in scope, and must work this cycle, is a real policy-enforcing sandbox (network egress, shell/exec, and filesystem access) proven by the containment attack suite: containment is our guarantee to demonstrate now, distinct from the managed substrate product (density, warm-pooling, cold-start) we defer. Out of scope: building the managed substrate product ourselves.
5. **Extend, do not fork, governance.** Build on the shipped OPA/Rego policy engine and planned governance capabilities. Out of scope: re-implementing authorization or standing up a competing model.
6. **Expect churn.** The feature definition and execution-plane interface may change again. Keep both the "what we consume" and "how we govern" surfaces open behind the abstraction rather than hard-committing now.

**Invariants the architecture must hold**, regardless of which candidates win:

- **Separation of reasoning and execution.** Agents reason, recommend, and act; deterministic workflows validate and execute. Every state-changing action an agent takes, whether a direct tool call or a workflow invocation, passes the same governance gate (policy decision, audit, sandbox, credential-at-the-boundary) as deterministic automation, and the most consequential changes run through governed workflows.
- **One governance plane.** A single attribute-based policy engine (OPA/Rego), one set of approvals, and one audit trail span both agent and deterministic automation. No parallel governance model.
- **Containment.** Agent workloads are isolated from deterministic action nodes, and agents never hold automation credentials or model API keys directly.
- **Workflows as agent actions.** Deterministic workflows are exposed to agents as governed, callable actions. Invoking a workflow and modifying its definition are separate permissions: the governance gate can let an agent trigger a workflow while denying any change to its definition. Invoke-not-modify is the policy default, enforced rather than assumed.

**A risk we name but do not solve this cycle:**

- Authorization governs what a principal may do, not whether an agent has been manipulated into misusing a permitted action. Prompt injection and tool or MCP poisoning are a named risk this architecture must address in productization; this cycle proves the authorization and containment gate works, not that the agent's reasoning is trustworthy.

**Also out of scope, and not the inverse of any principle above:**

- Production hardening or GA-quality code. This is architecture proof, harvestable at best.
- The custom policy-authoring UI and external secrets managers. Separate tracks, not this effort.
- Cost management (LLM spend at fleet scale). A real productization concern, deferred this cycle.
- Failure, rollback, and compensation of agent-triggered workflows. The "governed, not forbidden" stance lets an agent trigger a workflow that may half-execute on bad reasoning; bounding that blast radius is deferred to productization, not proven this cycle.

---

## 6. Stakeholders and alignment surface

- **Product and architecture**: owns the abstraction, governance surface, and product direction.
- **Execution-plane maintainers**: own the sandbox substrate and cold-start solution; the prototype aligns its substrate seam with theirs.
- **Node-type maintainers**: own node types, distribution, and the SDK.
- **Policy and governance maintainers**: own the OPA/Rego engine extended to agent actions.
- **Product leadership**: owns product-alignment decisions and decides whether the prototype should be productized.
- **Engineering leads**: own architectural alignment across the platform.
