"""Validated model API settings loaded from the process environment."""

import os

from pydantic import BaseModel, Field, SecretStr


class _ProviderSettings(BaseModel):
    """Shared connection settings, with masked credentials and bounded calls."""

    api_key: SecretStr = Field(min_length=1)
    timeout_seconds: float = Field(default=30, gt=0)


class GeminiSettings(_ProviderSettings):
    """Google AI Studio settings; Argon is the hackathon account's default."""

    model: str = Field(default="gemini-4-argon", min_length=1)

    @classmethod
    def from_env(cls) -> "GeminiSettings":
        """Load GEMINI_API_KEY and optional GEMINI_MODEL."""
        return cls.model_validate(
            {
                "api_key": os.environ.get("GEMINI_API_KEY"),
                "model": os.environ.get("GEMINI_MODEL", "gemini-4-argon"),
            }
        )


class SubQSettings(_ProviderSettings):
    """SubQ Snap settings for the typed Decisions API."""

    model: str = Field(default="subq-snap-1.1", min_length=1)

    @classmethod
    def from_env(cls) -> "SubQSettings":
        """Load SUBQ_API_KEY and optional SUBQ_MODEL."""
        return cls.model_validate(
            {
                "api_key": os.environ.get("SUBQ_API_KEY"),
                "model": os.environ.get("SUBQ_MODEL", "subq-snap-1.1"),
            }
        )
