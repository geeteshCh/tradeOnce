"""Typed synchronous client for Gemini content generation."""

from types import TracebackType
from typing import Self, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from providers.config import GeminiSettings

TModel = TypeVar("TModel", bound=BaseModel)


class GeminiClient:
    """Generate plain text or validated Pydantic models with Gemini.

    The client uses the explicitly configured API key so ambient Google API
    credentials cannot silently select a different account. SDK transport and
    API errors propagate unchanged to preserve their diagnostic information.
    """

    def __init__(self, settings: GeminiSettings) -> None:
        """Create a client from validated Gemini connection settings."""
        self._settings = settings
        self._client = genai.Client(
            api_key=settings.api_key.get_secret_value(),
            http_options=types.HttpOptions(
                api_version="v1beta",
                timeout=int(settings.timeout_seconds * 1_000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    @classmethod
    def from_env(cls) -> Self:
        """Create a client from the environment-backed Gemini settings."""
        return cls(GeminiSettings.from_env())

    def generate_text(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        max_output_tokens: int = 4096,
    ) -> str:
        """Generate text for a prompt, raising if Gemini returns no text."""
        response = self._client.models.generate_content(
            model=self._settings.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                max_output_tokens=max_output_tokens,
            ),
        )
        text = response.text
        if text is None or not text.strip():
            raise ValueError("Gemini returned an empty text response")
        return text

    def generate_structured(
        self,
        prompt: str,
        response_model: type[TModel],
        *,
        system_instruction: str | None = None,
        max_output_tokens: int = 4096,
    ) -> TModel:
        """Generate and validate a response as the requested Pydantic model."""
        response = self._client.models.generate_content(
            model=self._settings.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                max_output_tokens=max_output_tokens,
                response_mime_type="application/json",
                response_schema=response_model,
            ),
        )
        parsed = response.parsed
        if not isinstance(parsed, response_model):
            raise TypeError(
                f"Gemini returned no parsed {response_model.__name__} response"
            )
        return parsed

    def close(self) -> None:
        """Release the SDK client's network resources."""
        self._client.close()

    def __enter__(self) -> Self:
        """Return this client for use in a synchronous context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the SDK client when leaving a context manager."""
        self.close()
