"""Small synchronous client for SubQ Snap's typed Decisions API."""

from types import TracebackType
from typing import Self

import httpx

from providers.config import SubQSettings
from providers.subq_models import (
    BinaryQuestion,
    DecisionResponse,
    DecisionState,
    _DecisionRequest,
)

SUBQ_BASE_URL = "https://api.subq.ai/v1/"


class SubQClient:
    """Reuse one HTTP connection pool; each evaluation makes a single attempt."""

    def __init__(
        self, settings: SubQSettings, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._model = settings.model
        self._client = httpx.Client(
            base_url=SUBQ_BASE_URL,
            headers={"Authorization": f"Bearer {settings.api_key.get_secret_value()}"},
            timeout=settings.timeout_seconds,
            transport=transport,
        )

    @classmethod
    def from_env(cls) -> Self:
        """Read SUBQ_API_KEY and optional SUBQ_MODEL from the environment."""
        return cls(SubQSettings.from_env())

    def evaluate(
        self, state: DecisionState, questions: dict[str, BinaryQuestion]
    ) -> DecisionResponse:
        """Score rubric questions against text or JSON; HTTP errors propagate."""
        request = _DecisionRequest(model=self._model, state=state, questions=questions)
        response = self._client.post("systemone", json=request.model_dump(mode="json"))
        response.raise_for_status()
        result = DecisionResponse.model_validate(response.json())
        if result.answers.keys() != questions.keys():
            raise ValueError("SubQ answer names do not match the requested questions.")
        return result

    def close(self) -> None:
        """Release the client's HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
