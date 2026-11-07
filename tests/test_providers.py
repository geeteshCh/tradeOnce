import json
import os
import unittest
from unittest.mock import MagicMock, patch

import httpx
from google.genai import types
from pydantic import BaseModel, ValidationError

from providers.config import GeminiSettings, SubQSettings
from providers.gemini import GeminiClient
from providers.subq import SubQClient
from providers.subq_models import BinaryCriteria, BinaryQuestion


class StructuredAnswer(BaseModel):
    """Small typed response used to exercise Gemini JSON parsing."""

    answer: str


def gemini_text_response(text: str) -> types.GenerateContentResponse:
    """Build a native SDK response containing one text part."""
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(parts=[types.Part(text=text)]))
        ]
    )


class ProviderSettingsTests(unittest.TestCase):
    def test_settings_load_model_overrides_and_mask_keys(self):
        cases = (
            (
                GeminiSettings,
                "GEMINI_API_KEY",
                "GEMINI_MODEL",
                "gemini-test-model",
            ),
            (SubQSettings, "SUBQ_API_KEY", "SUBQ_MODEL", "subq-test-model"),
        )

        for settings_type, key_name, model_name, model_value in cases:
            with self.subTest(settings_type=settings_type.__name__):
                env = {key_name: "private-test-key", model_name: model_value}
                with patch.dict(os.environ, env, clear=True):
                    settings = settings_type.from_env()

                self.assertEqual(settings.model, model_value)
                self.assertNotIn(env[key_name], repr(settings))
                self.assertIn("**********", repr(settings))

    def test_settings_require_api_keys(self):
        for settings_type in (GeminiSettings, SubQSettings):
            with (
                self.subTest(settings_type=settings_type.__name__),
                patch.dict(os.environ, {}, clear=True),
                self.assertRaises(ValidationError),
            ):
                settings_type.from_env()


class GeminiClientTests(unittest.TestCase):
    def setUp(self):
        self.sdk_client = MagicMock()
        client_patch = patch(
            "providers.gemini.genai.Client", return_value=self.sdk_client
        )
        self.addCleanup(client_patch.stop)
        self.client_factory = client_patch.start()
        self.settings = GeminiSettings(
            api_key="gemini-test-key",
            model="gemini-test-model",
            timeout_seconds=1.25,
        )
        self.client = GeminiClient(self.settings)
        self.addCleanup(self.client.close)

    def test_initializes_explicit_v1beta_single_attempt_client(self):
        kwargs = self.client_factory.call_args.kwargs
        options = kwargs["http_options"]

        self.assertEqual(kwargs["api_key"], "gemini-test-key")
        self.assertEqual(options.api_version, "v1beta")
        self.assertEqual(options.timeout, 1250)
        self.assertEqual(options.retry_options.attempts, 1)

    def test_generate_text_returns_native_response_text_and_configures_request(self):
        self.sdk_client.models.generate_content.return_value = gemini_text_response(
            " concise answer "
        )

        result = self.client.generate_text(
            "question",
            system_instruction="be concise",
            max_output_tokens=123,
        )

        self.assertEqual(result, " concise answer ")
        kwargs = self.sdk_client.models.generate_content.call_args.kwargs
        self.assertEqual(kwargs["model"], "gemini-test-model")
        self.assertEqual(kwargs["contents"], "question")
        self.assertEqual(kwargs["config"].system_instruction, "be concise")
        self.assertEqual(kwargs["config"].max_output_tokens, 123)

    def test_generate_structured_returns_typed_native_parsed_value(self):
        expected = StructuredAnswer(answer="yes")
        self.sdk_client.models.generate_content.return_value = (
            types.GenerateContentResponse(parsed=expected)
        )

        result = self.client.generate_structured(
            "answer as JSON", StructuredAnswer, max_output_tokens=321
        )

        self.assertEqual(result, expected)
        config = self.sdk_client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertIs(config.response_schema, StructuredAnswer)
        self.assertEqual(config.max_output_tokens, 321)

    def test_empty_text_and_missing_or_wrong_parsed_values_are_rejected(self):
        for text in ("", "   "):
            with self.subTest(text=text):
                self.sdk_client.models.generate_content.return_value = (
                    gemini_text_response(text)
                )
                with self.assertRaises(ValueError):
                    self.client.generate_text("question")

        for parsed in (None, BinaryCriteria(true="yes", false="no")):
            with self.subTest(parsed=parsed):
                self.sdk_client.models.generate_content.return_value = (
                    types.GenerateContentResponse(parsed=parsed)
                )
                with self.assertRaises(TypeError):
                    self.client.generate_structured("question", StructuredAnswer)

    def test_sdk_failure_propagates_after_one_generation_call(self):
        error = RuntimeError("provider unavailable")
        self.sdk_client.models.generate_content.side_effect = error

        with self.assertRaises(RuntimeError) as raised:
            self.client.generate_text("question")

        self.assertIs(raised.exception, error)
        self.sdk_client.models.generate_content.assert_called_once()

    def test_context_manager_closes_sdk_client(self):
        with GeminiClient(self.settings) as client:
            self.assertIsInstance(client, GeminiClient)

        self.sdk_client.close.assert_called_once()


class SubQClientTests(unittest.TestCase):
    question = BinaryQuestion(
        instructions="Determine whether the rollout is safe.",
        criteria=BinaryCriteria(
            true="All invariants pass.",
            false="At least one invariant fails.",
        ),
    )

    def make_client(self, handler) -> SubQClient:
        client = SubQClient(
            SubQSettings(
                api_key="subq-test-key",
                model="subq-test-model",
                timeout_seconds=0.25,
            ),
            transport=httpx.MockTransport(handler),
        )
        self.addCleanup(client.close)
        return client

    @staticmethod
    def valid_response(
        *, answers: dict[str, dict[str, object]] | None = None
    ) -> dict[str, object]:
        return {
            "model": "subq-test-model",
            "answers": answers
            if answers is not None
            else {"safe": {"type": "noul", "noul": 0.75}},
            "usage": {"input_tokens": 12, "output_tokens": 4},
        }

    def test_evaluate_sends_exact_authenticated_request_and_parses_alias(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=self.valid_response())

        client = self.make_client(handler)
        result = client.evaluate(
            {"release": "candidate_a", "checks": ["cash", "position"]},
            {"safe": self.question},
        )

        self.assertEqual(result.answers["safe"].probability_yes, 0.75)
        self.assertEqual(result.usage.input_tokens, 12)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(str(request.url), "https://api.subq.ai/v1/systemone")
        self.assertEqual(request.headers["Authorization"], "Bearer subq-test-key")
        self.assertEqual(
            json.loads(request.content),
            {
                "model": "subq-test-model",
                "state": {
                    "release": "candidate_a",
                    "checks": ["cash", "position"],
                },
                "questions": {
                    "safe": {
                        "type": "noul",
                        "instructions": "Determine whether the rollout is safe.",
                        "criteria": {
                            "true": "All invariants pass.",
                            "false": "At least one invariant fails.",
                        },
                    }
                },
            },
        )

    def test_question_count_is_validated_before_transport(self):
        handler = MagicMock()
        client = self.make_client(handler)

        for questions in ({}, {str(index): self.question for index in range(129)}):
            with (
                self.subTest(question_count=len(questions)),
                self.assertRaises(ValidationError),
            ):
                client.evaluate("state", questions)

        handler.assert_not_called()

    def test_invalid_provider_responses_are_rejected(self):
        cases = (
            self.valid_response(answers={"safe": {"type": "noul", "noul": 1.01}}),
            self.valid_response(answers={"different": {"type": "noul", "noul": 0.5}}),
        )

        for response_body in cases:
            with self.subTest(response_body=response_body):
                client = self.make_client(
                    lambda request, body=response_body: httpx.Response(200, json=body)
                )
                with self.assertRaises((ValidationError, ValueError)):
                    client.evaluate("state", {"safe": self.question})

    def test_http_non_json_and_timeout_failures_propagate_without_retry(self):
        calls = 0

        def failing_handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("timed out", request=request)

        timeout_client = self.make_client(failing_handler)
        with self.assertRaises(httpx.ReadTimeout):
            timeout_client.evaluate("state", {"safe": self.question})
        self.assertEqual(calls, 1)

        cases = (
            httpx.Response(503, json={"error": "unavailable"}),
            httpx.Response(200, content=b"not-json"),
        )
        for response in cases:
            with self.subTest(status=response.status_code):
                request_count = 0

                def handler(
                    request: httpx.Request, response: httpx.Response = response
                ) -> httpx.Response:
                    nonlocal request_count
                    request_count += 1
                    return response

                client = self.make_client(handler)
                with self.assertRaises((httpx.HTTPStatusError, json.JSONDecodeError)):
                    client.evaluate("state", {"safe": self.question})
                self.assertEqual(request_count, 1)

    def test_context_manager_closes_http_client(self):
        with self.make_client(
            lambda request: httpx.Response(200, json=self.valid_response())
        ) as client:
            self.assertFalse(client._client.is_closed)

        self.assertTrue(client._client.is_closed)


if __name__ == "__main__":
    unittest.main()
