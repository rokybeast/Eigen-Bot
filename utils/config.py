"""Configuration management using Pydantic settings."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Bot configuration settings."""

    discord_token: str = Field(default='demo_token')
    # Backwards compatible single guild id
    guild_id: Optional[int] = Field(default=None)
    # Preferred: comma-separated list (or JSON list) of guild ids for fast per-guild slash-command sync
    guild_ids: list[int] = Field(default_factory=list)
    log_level: str = Field(default='INFO')
    owner_id: Optional[int] = Field(default=None)
    topgg_token: Optional[str] = Field(default=None)
    topgg_webhook_secret: Optional[str] = Field(default=None)
    redis_url: Optional[str] = Field(default=None)

    # CodeBuddy settings
    question_channel_id: Optional[int] = Field(default=None)

    model_config = SettingsConfigDict(
        env_file='.env',
        case_sensitive=False,
        extra='ignore'  # Ignore extra fields from .env
    )

    @field_validator('guild_ids', mode='before')
    @classmethod
    def _parse_guild_ids(cls, v: Any) -> list[int]:
        """Parse guild ids from env.

        Supports:
        - unset / empty -> []
        - JSON list: "[1,2]"
        - CSV: "1,2,3"
        """
        if v is None:
            return []
        if isinstance(v, list):
            return [int(x) for x in v if str(x).strip()]
        if isinstance(v, (int, str)):
            s = str(v).strip()
            if not s:
                return []
            # JSON list
            if s.startswith('[') and s.endswith(']'):
                try:
                    import json

                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return [int(x) for x in parsed]
                except Exception:
                    # Fall back to CSV parsing
                    pass
            # CSV
            parts = [p.strip() for p in s.split(',')]
            return [int(p) for p in parts if p]
        raise TypeError('guild_ids must be a list, int, or string')

    @model_validator(mode='after')
    def _coerce_single_guild_to_list(self) -> 'Config':
        # If only legacy GUILD_ID is set, treat it as a single-item list.
        if not self.guild_ids and self.guild_id:
            self.guild_ids = [int(self.guild_id)]
        return self
