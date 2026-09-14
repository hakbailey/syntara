"""The agent governance gate: an MCP tool-call proxy and LLM completion broker.

Lives in the `syntara` API process (where the Rego evaluator is already
initialized), not the `temporal-worker` process where the agent loop runs
today. This is what lets a sandboxed engine (Prototype A3) reach the outside
world only through here: every tool call is authorized and audited before
being forwarded, and every credential (MCP bearer token, LLM API key) is
resolved and injected here so the caller never holds it.
"""
