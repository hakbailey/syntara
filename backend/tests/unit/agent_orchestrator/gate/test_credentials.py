"""Unit tests for the gate's `resolve_credential_field` credential resolver.

Exercises the same lookup -> decrypt -> resolve-injectors -> extract-field
flow as `InvocationExecutor._resolve_credential`, mirrored here (not reused
from there) — see `agent_orchestrator/gate/credentials.py`'s module docstring
for why.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from syntara.agent_orchestrator.exceptions import CredentialResolutionError
from syntara.agent_orchestrator.gate.credentials import resolve_credential_field


def _mock_credential_and_type(
    *,
    enabled: bool = True,
    has_secret: bool = True,
    has_cred_type: bool = True,
) -> tuple[MagicMock, MagicMock | None]:
    mock_credential = MagicMock()
    mock_credential.enabled = enabled
    mock_credential.secret_id = uuid4() if has_secret else None
    mock_credential.credential_type_id = uuid4()

    mock_cred_type = MagicMock() if has_cred_type else None
    if mock_cred_type:
        mock_cred_type.injectors = {}

    return mock_credential, mock_cred_type


def _session_get_dispatch(mock_credential: MagicMock | None, mock_cred_type: MagicMock | None) -> AsyncMock:
    async def _get(model_class: type, _pk: object) -> object | None:
        from syntara.credentials.models.credential import Credential
        from syntara.credentials.models.credential_type import CredentialType

        if model_class is Credential:
            return mock_credential
        if model_class is CredentialType:
            return mock_cred_type
        return None

    return AsyncMock(side_effect=_get)


class TestResolveCredentialField:
    """Tests for resolve_credential_field's error branches and happy path."""

    async def test_invalid_uuid_raises(self) -> None:
        session = MagicMock()
        with pytest.raises(CredentialResolutionError, match="Invalid execution credential ID"):
            await resolve_credential_field(
                session, "not-a-uuid", field_name="bearer_token", label="execution credential"
            )

    async def test_not_found_raises(self) -> None:
        session = MagicMock()
        session.get = _session_get_dispatch(None, None)
        with pytest.raises(CredentialResolutionError, match="not found"):
            await resolve_credential_field(
                session, str(uuid4()), field_name="bearer_token", label="execution credential"
            )

    async def test_disabled_raises(self) -> None:
        session = MagicMock()
        mock_credential, mock_cred_type = _mock_credential_and_type(enabled=False)
        session.get = _session_get_dispatch(mock_credential, mock_cred_type)
        with pytest.raises(CredentialResolutionError, match="disabled"):
            await resolve_credential_field(
                session, str(uuid4()), field_name="bearer_token", label="execution credential"
            )

    async def test_no_secret_raises(self) -> None:
        session = MagicMock()
        mock_credential, mock_cred_type = _mock_credential_and_type(has_secret=False)
        session.get = _session_get_dispatch(mock_credential, mock_cred_type)
        with pytest.raises(CredentialResolutionError, match="no stored secret"):
            await resolve_credential_field(
                session, str(uuid4()), field_name="bearer_token", label="execution credential"
            )

    async def test_happy_path_returns_field_value(self) -> None:
        session = MagicMock()
        mock_credential, mock_cred_type = _mock_credential_and_type()
        session.get = _session_get_dispatch(mock_credential, mock_cred_type)

        mock_resolved = MagicMock()
        mock_resolved.extra_vars = {"bearer_token": "tok-abc-123"}

        with (
            patch("syntara.agent_orchestrator.gate.credentials.create_secret_service") as mock_secret_svc,
            patch("syntara.agent_orchestrator.gate.credentials.InjectorResolver") as mock_injector,
        ):
            mock_secret_svc.return_value.retrieve_secret = AsyncMock(return_value={"token": "encrypted"})
            mock_injector.resolve.return_value = mock_resolved

            result = await resolve_credential_field(
                session, str(uuid4()), field_name="bearer_token", label="execution credential"
            )

        assert result == "tok-abc-123"

    async def test_field_absent_returns_none(self) -> None:
        session = MagicMock()
        mock_credential, mock_cred_type = _mock_credential_and_type()
        session.get = _session_get_dispatch(mock_credential, mock_cred_type)

        mock_resolved = MagicMock()
        mock_resolved.extra_vars = {}

        with (
            patch("syntara.agent_orchestrator.gate.credentials.create_secret_service") as mock_secret_svc,
            patch("syntara.agent_orchestrator.gate.credentials.InjectorResolver") as mock_injector,
        ):
            mock_secret_svc.return_value.retrieve_secret = AsyncMock(return_value={"token": "encrypted"})
            mock_injector.resolve.return_value = mock_resolved

            result = await resolve_credential_field(
                session, str(uuid4()), field_name="bearer_token", label="execution credential"
            )

        assert result is None
