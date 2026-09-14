"""Seed a realistic-but-small identity/policy volume via the Syntara API.

Drives the same endpoints backend/tools/authz_cli.py drives (create-user/-project/-group/
-role/-policy, role_assignments) so the later q7 latency measurement exercises a real DB
fan-out in authz/resolver.py, not a code-resolved builtin lookup. Custom (not builtin)
roles/policies are required for that — builtin ones cost zero DB rows.

Scale is controlled by env vars — see config.IncidentVolume.from_env(). Idempotent: re-running
with the same PROTOTYPE_INCIDENT_SEED does not create duplicates (entities are looked up by
name first; relationship rows tolerate 409/400 "already exists" and are skipped).

Usage: python seed_volume.py
"""

from __future__ import annotations

import secrets
import sys

from api_client import ApiClient, get_or_create, post_idempotent
from config import IncidentVolume
from fixtures.names import group_name, policy_name, policy_statement, project_name, rng, role_name, user


def seed_projects(client: ApiClient, volume: IncidentVolume) -> list[str]:
    sys.stdout.write(f"Seeding {volume.projects} projects...\n")
    return [
        get_or_create(
            client,
            list_path="/projects",
            list_params={"name": (name := project_name(i))},
            match={"name": name},
            create_path="/projects",
            create_body={"name": name, "description": "prototype-incident volume seed"},
            label=f"project {name}",
        )["id"]
        for i in range(volume.projects)
    ]


def seed_users(client: ApiClient, volume: IncidentVolume) -> list[str]:
    sys.stdout.write(f"Seeding {volume.users} users...\n")
    ids = []
    for i in range(volume.users):
        username, email, full_name = user(i)
        u = get_or_create(
            client,
            list_path="/users",
            list_params={"username": username},
            match={"username": username},
            create_path="/users",
            create_body={
                "username": username,
                "email": email,
                "full_name": full_name,
                "password": secrets.token_urlsafe(32),
            },
            label=f"user {username}",
        )
        ids.append(u["id"])
    return ids


def seed_groups(client: ApiClient, volume: IncidentVolume, user_ids: list[str], r) -> list[str]:
    """The first user (fixtures.names.user(0)) is deliberately over-connected — put in
    every group — so it's a realistic "heavy" reference principal for the q7 timing
    baseline in verify.py, rather than an average-case principal."""
    sys.stdout.write(f"Seeding {volume.groups} groups (with members)...\n")
    reference_user_id = user_ids[0]
    ids = []
    for i in range(volume.groups):
        name = group_name(i)
        g = get_or_create(
            client,
            list_path="/groups",
            list_params={"name": name},
            match={"name": name},
            create_path="/groups",
            create_body={"name": name, "description": "prototype-incident volume seed"},
            label=f"group {name}",
        )
        ids.append(g["id"])
        members = {reference_user_id, *r.sample(user_ids, k=min(10, len(user_ids)))}
        for member_id in members:
            post_idempotent(client, f"/groups/{g['id']}/members", {"user_id": member_id})
    return ids


def seed_policies(
    client: ApiClient, volume: IncidentVolume, project_ids: list[str], r
) -> tuple[list[str], dict[str, list[str]]]:
    """Returns (system_policy_names, {project_id: [project-scoped policy names]}).

    A policy's scope is fixed at creation (by whether project_id is set) and its
    statements' scope must match: system policies use "any", project policies must use
    "project"/"own" — confirmed live ("Project policies only accept scope='project' or
    'own', got scope='any'"). Split roughly half/half so both a global-role and a
    project-role fan-out are real for the q7 DB join measurement, not just one shape."""
    sys.stdout.write(f"Seeding {volume.custom_policies} custom policies...\n")
    system_names: list[str] = []
    project_names: dict[str, list[str]] = {pid: [] for pid in project_ids}
    for i in range(volume.custom_policies):
        name = policy_name(i)
        is_project_scoped = i % 2 == 0 and project_ids
        if is_project_scoped:
            project_id = project_ids[i % len(project_ids)]
            create_body = {
                "name": name,
                "project_id": project_id,
                "description": "prototype-incident volume seed",
                "statements": [policy_statement(r, scope="project") for _ in range(r.randint(1, 2))],
            }
        else:
            project_id = None
            create_body = {
                "name": name,
                "description": "prototype-incident volume seed",
                "statements": [policy_statement(r, scope="any") for _ in range(r.randint(1, 2))],
            }
        get_or_create(
            client,
            list_path="/policies",
            list_params={"name": name},
            match={"name": name},
            create_path="/policies",
            create_body=create_body,
            label=f"policy {name}" + (f" (project {project_id})" if project_id else ""),
        )
        (project_names[project_id] if project_id else system_names).append(name)
    return system_names, project_names


def seed_roles(
    client: ApiClient,
    volume: IncidentVolume,
    system_policy_names: list[str],
    project_policy_names: dict[str, list[str]],
    r,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Returns (system_role_names, [(project-scoped role name, its project_id), ...]).

    A project-scoped role can only reference policies belonging to that same project
    ("Policies not found in project ...", confirmed live), so roles and policies are
    paired by project, not mixed freely."""
    sys.stdout.write(f"Seeding {volume.custom_roles} custom roles...\n")
    project_ids = [pid for pid, names in project_policy_names.items() if names]
    system_names: list[str] = []
    project_roles: list[tuple[str, str]] = []
    for i in range(volume.custom_roles):
        name = role_name(i)
        is_project_scoped = i % 2 == 0 and project_ids
        if is_project_scoped:
            project_id = project_ids[i % len(project_ids)]
            pool = project_policy_names[project_id]
            attached = r.sample(pool, k=min(r.randint(1, 3), len(pool)))
            create_body = {
                "name": name,
                "project_id": project_id,
                "description": "prototype-incident volume seed",
                "policies": attached,
            }
        else:
            project_id = None
            attached = r.sample(system_policy_names, k=min(r.randint(1, 3), len(system_policy_names)))
            create_body = {"name": name, "description": "prototype-incident volume seed", "policies": attached}
        get_or_create(
            client,
            list_path="/roles",
            list_params={"name": name},
            match={"name": name},
            create_path="/roles",
            create_body=create_body,
            label=f"role {name}" + (f" (project {project_id})" if project_id else ""),
        )
        if project_id:
            project_roles.append((name, project_id))
        else:
            system_names.append(name)
    return system_names, project_roles


def seed_role_assignments(
    client: ApiClient,
    volume: IncidentVolume,
    system_role_names: list[str],
    project_roles: list[tuple[str, str]],
    user_ids: list[str],
    group_ids: list[str],
    r,
) -> None:
    """A project-scoped role can only be assigned within its own project (a system role
    assigned via /projects/{id}/role_assignments — or a project role assigned to a
    different project — both 422 "is a system role and cannot be assigned to a
    project"/"not found in project", confirmed live). So the role and the assignment
    path/project are chosen together, not independently."""
    sys.stdout.write(f"Seeding {volume.role_assignments} role assignments...\n")
    reference_user_id = user_ids[0]
    created = 0
    for _ in range(volume.role_assignments):
        # Mix principal types (user/group); bias ~15% of assignments directly onto the
        # reference principal so it's genuinely over-connected (see seed_groups), not
        # just an average-case user.
        body: dict[str, object] = {}
        if r.random() < 0.15:
            body["principal_id"] = reference_user_id
        elif r.random() < 0.6:
            body["principal_id"] = r.choice(user_ids)
        else:
            body["group_id"] = r.choice(group_ids)

        if project_roles and r.random() < 0.7:
            role_name_, project_id = r.choice(project_roles)
            body["role_name"] = role_name_
            path = f"/projects/{project_id}/role_assignments"
        else:
            body["role_name"] = r.choice(system_role_names)
            path = "/role_assignments"

        result = post_idempotent(client, path, body)
        if result is not None:
            created += 1
    sys.stdout.write(f"  created {created} new assignments (rest already existed)\n")


def main() -> None:
    volume = IncidentVolume.from_env()
    r = rng(volume.seed)
    client = ApiClient()

    project_ids = seed_projects(client, volume)
    user_ids = seed_users(client, volume)
    group_ids = seed_groups(client, volume, user_ids, r)
    system_policy_names, project_policy_names = seed_policies(client, volume, project_ids, r)
    system_role_names, project_roles = seed_roles(client, volume, system_policy_names, project_policy_names, r)
    seed_role_assignments(client, volume, system_role_names, project_roles, user_ids, group_ids, r)

    sys.stdout.write("Volume seed complete.\n")


if __name__ == "__main__":
    main()
