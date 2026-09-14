"""Credential resolution for the agent governance gate.

Deliberately independent of `InvocationExecutor`'s equivalent resolver
(`_resolve_credential`): that class's tests patch `create_secret_service`/
`InjectorResolver` at `invocation_executor`'s own module import path, so a
shared helper imported from elsewhere would silently stop being mocked by
those tests. This is a small, disclosed duplication of the same lookup ->
decrypt -> resolve-injectors -> extract-field flow, scoped to the gate, not
a design flaw — see `prototype-plan/agentic-orchestration-prototyping-status.md`.
"""

from uuid import UUID

from sqlmodel.ext.asyncio.session import AsyncSession

from syntara.agent_orchestrator.exceptions import CredentialResolutionError
from syntara.core.services.secret_service import create_secret_service
from syntara.credentials.lib.injector_resolver import InjectorResolver
from syntara.credentials.models.credential import Credential
from syntara.credentials.models.credential_type import CredentialType


async def resolve_credential_field(
    session: AsyncSession,
    credential_id: str,
    *,
    field_name: str,
    label: str,
) -> str | None:
    """Look up, decrypt, and extract one field from a credential's resolved injectors.

    The raw secret only ever exists transiently inside this function — callers
    receive the extracted field value, never the `Credential`/decrypted payload.

    Raises:
        CredentialResolutionError: If the credential cannot be found, is
            disabled, has no stored secret, or fails to decrypt/resolve.

    """
    cap_label = f"{label[0].upper()}{label[1:]}"
    try:
        cred_uuid = UUID(credential_id)
    except ValueError as e:
        msg = f"Invalid {label} ID '{credential_id}'."
        raise CredentialResolutionError(msg) from e

    credential = await session.get(Credential, cred_uuid)
    if not credential:
        msg = f"{cap_label} '{credential_id}' not found."
        raise CredentialResolutionError(msg)
    if not credential.enabled:
        msg = f"{cap_label} '{credential_id}' is disabled."
        raise CredentialResolutionError(msg)
    if not credential.secret_id:
        msg = f"{cap_label} '{credential_id}' has no stored secret data."
        raise CredentialResolutionError(msg)

    try:
        secret_service = create_secret_service(session)
        decrypted = await secret_service.retrieve_secret(credential.secret_id)
    except Exception as e:
        msg = f"Failed to decrypt {label} '{credential_id}'."
        raise CredentialResolutionError(msg) from e

    cred_type = await session.get(CredentialType, credential.credential_type_id)
    if not cred_type:
        msg = f"Credential type for {label} '{credential_id}' not found."
        raise CredentialResolutionError(msg)

    try:
        resolved = InjectorResolver.resolve(cred_type.injectors, decrypted)
    except Exception as e:
        msg = f"Failed to resolve {label} '{credential_id}' injector templates."
        raise CredentialResolutionError(msg) from e

    return resolved.extra_vars.get(field_name)
