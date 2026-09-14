"""Minimal settings for the isolated harness process."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class HarnessSettings(BaseSettings):
    """Infrastructure settings that contain no provider or tool credentials."""

    model_config = SettingsConfigDict(env_prefix="HARNESS_", case_sensitive=False)

    gate_base_url: str = Field(default="https://agent-gate:8000/api/v1/agent-gate")
    gate_ca_path: str = Field(default="/run/trust/agent-gate-ca.pem")
    request_timeout_seconds: float = Field(default=300.0, gt=0)


harness_settings = HarnessSettings()
