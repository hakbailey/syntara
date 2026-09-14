"""Deterministic name generators for the bulk volume seed.

Deterministic (seeded RNG) so re-running seed_volume.py with the same PROTOTYPE_INCIDENT_SEED
produces the same names, which is what makes the get_or_create idempotency checks in
api_client.py actually skip re-creation on a second run.
"""

from __future__ import annotations

import random

_TEAMS = [
    "platform",
    "checkout",
    "payments",
    "data",
    "sre",
    "security",
    "mobile",
    "growth",
    "identity",
    "networking",
]

# integration:read is valid at system scope but NOT at project scope — confirmed live
# ("Resource type 'integration' is not valid at project scope. Allowed: approval,
# credential, execution, files, invocation, policy, project, role, role-assignment,
# service_account, workflow"). _PROJECT_ACTIONS is _ACTIONS minus that one exception,
# not a hand-picked list, so it can't silently drift from _ACTIONS as that list grows.
_ACTIONS = [
    "workflow:create",
    "workflow:read",
    "workflow:update",
    "workflow:delete",
    "credential:read",
    "credential:create",
    "integration:read",
    "project:read",
    "execution:read",
    "execution:run",
]
_PROJECT_ACTIONS = [a for a in _ACTIONS if not a.startswith("integration:")]


def rng(seed: int) -> random.Random:
    return random.Random(seed)


def project_name(i: int) -> str:
    return f"incident-vol-project-{i:03d}"


def user(i: int) -> tuple[str, str, str]:
    """Returns (username, email, full_name)."""
    username = f"incident-vol-user-{i:04d}"
    return username, f"{username}@example.com", f"Volume User {i:04d}"


def group_name(i: int) -> str:
    team = _TEAMS[i % len(_TEAMS)]
    return f"incident-vol-group-{team}-{i:03d}"


def role_name(i: int) -> str:
    return f"incident-vol-role-{i:03d}"


def policy_name(i: int) -> str:
    return f"incident-vol-policy-{i:03d}"


def policy_statement(r: random.Random, *, scope: str) -> dict[str, object]:
    # effect is always "allow" — confirmed live against the API that "deny" is rejected
    # Explicit deny-effect policies are unavailable in the current API. See
    # seed_scenario.py's seed_deny_policy docstring for how the reference task's
    # "denied action" goal is met with default-deny and no explicit deny statement.
    #
    # scope is passed in, not chosen here, because it must match the policy's own scope
    # (system vs. project-scoped) — confirmed live that a project-scoped policy rejects
    # scope="any" ("Project policies only accept scope='project' or 'own'").
    pool = _PROJECT_ACTIONS if scope == "project" else _ACTIONS
    actions = r.sample(pool, k=r.randint(1, min(3, len(pool))))
    return {"effect": "allow", "actions": actions, "scope": scope}
