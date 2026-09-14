"""Thin authenticated HTTP client for driving Syntara purely as an external API consumer.

Deliberately re-derives the small login/request pattern already used by
backend/tools/authz_cli.py and backend/tools/register_mcp_provider.py rather than
importing either — this prototype has zero dependency on those files' internal
structure, in exchange for ~20 lines of duplication. See prototype-incident/README.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx

BASE_URL = os.environ.get("PROTOTYPE_INCIDENT_API_BASE_URL", "https://localhost:8000/api/v1")


def _admin_password() -> str:
    password = os.environ.get("PROTOTYPE_INCIDENT_ADMIN_PASSWORD")
    if password:
        return password
    # Default resolves relative to this file, not CWD: these scripts run with CWD =
    # prototype-incident/ (via `uv run --project ../backend`, which points at that
    # project's environment without changing directory), so a bare relative default
    # like ".secrets/admin-password" would incorrectly look in prototype-incident/
    # instead of backend/, where `make secrets` actually generates it.
    default_path = Path(__file__).parent.parent / "backend" / ".secrets" / "admin-password"
    password_path = os.environ.get("APP_ADMIN_PASSWORD_PATH", str(default_path))
    return Path(password_path).read_text().strip()


class ApiClient:
    """Authenticated JSON client. Fails loudly (raise_for_status) — this is throwaway
    research tooling, not production error handling."""

    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url
        # Local dev API serves HTTPS with a self-signed cert (both `make dev` and the
        # containerized stack) — verify=False is the httpx equivalent of `curl -k`,
        # not a production posture.
        self._http = httpx.Client(verify=False)
        username = os.environ.get("PROTOTYPE_INCIDENT_ADMIN_USERNAME", "admin")
        response = self._http.post(
            f"{base_url}/auth/login", json={"username": username, "password": _admin_password()}
        )
        response.raise_for_status()
        self.token = response.json()["access_token"]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        r = self._http.get(f"{self.base_url}{path}", headers=self._headers(), **kwargs)
        r.raise_for_status()
        return r.json()  # type: ignore[no-any-return]

    def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        r = self._http.post(f"{self.base_url}{path}", headers=self._headers(), **kwargs)
        r.raise_for_status()
        return r.json() if r.content else {}  # type: ignore[no-any-return]

    def patch(self, path: str, **kwargs: Any) -> dict[str, Any]:
        r = self._http.patch(f"{self.base_url}{path}", headers=self._headers(), **kwargs)
        r.raise_for_status()
        return r.json() if r.content else {}  # type: ignore[no-any-return]


def find_one(resources: list[dict[str, Any]], **match: Any) -> dict[str, Any] | None:
    for r in resources:
        if all(r.get(k) == v for k, v in match.items()):
            return r
    return None


def post_idempotent(client: ApiClient, path: str, body: dict[str, Any], *, ignore_statuses: tuple[int, ...] = (400, 409)) -> dict[str, Any] | None:
    """POST that tolerates 'already exists'-style conflicts, for relationship rows
    (group membership, role assignments) that have no single natural id to look up first.

    Role-assignment duplicates are a special case: confirmed live that the API returns
    422 ("Role '...' is already assigned to user/group '...'"), not 400/409 like other
    duplicate-relationship checks in this codebase — an inconsistency in the API itself,
    not something to normalize away by broadening ignore_statuses to 422 generally (that
    would also swallow genuine validation errors, which is exactly the class of bug this
    prototype kept tripping over while building it). So this checks the message text for
    that one specific case instead of the status code alone."""
    r = client._http.post(f"{client.base_url}{path}", headers=client._headers(), json=body)
    if r.status_code in ignore_statuses:
        return None
    if r.status_code == 422 and "already assigned" in r.text:
        return None
    r.raise_for_status()
    return r.json() if r.content else {}  # type: ignore[no-any-return]


def get_or_create(
    client: ApiClient,
    *,
    list_path: str,
    list_params: dict[str, Any],
    match: dict[str, Any],
    create_path: str,
    create_body: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    """Idempotency helper: list + match before creating. Prints what it did."""
    existing = find_one(client.get(list_path, params=list_params)["resources"], **match)
    if existing:
        sys.stdout.write(f"  [exists] {label}: {existing['id']}\n")
        return existing
    created = client.post(create_path, json=create_body)
    sys.stdout.write(f"  [created] {label}: {created['id']}\n")
    return created
