import os
import unittest

from pydantic import BaseModel

from providers.config import GeminiSettings, SubQSettings
from providers.gemini import GeminiClient
from providers.subq import SubQClient
from providers.subq_models import BinaryCriteria, BinaryQuestion


class LiveStructuredAnswer(BaseModel):
    """Minimal schema for the live Gemini structured-output smoke test."""

    answer: str


@unittest.skipUnless(
    os.environ.get("RUN_MODEL_API_TESTS") == "1",
    "set RUN_MODEL_API_TESTS=1 to run live model API tests",
)
class GeminiLiveIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = GeminiClient(GeminiSettings.from_env())
        cls.addClassCleanup(cls.client.close)

    def test_argon_text_and_structured_generation(self):
        text = self.client.generate_text(
            "Reply with only the word ready.",
            max_output_tokens=1024,
        )
        structured = self.client.generate_structured(
            "Return JSON with an answer field set to ready.",
            LiveStructuredAnswer,
            max_output_tokens=1024,
        )

        self.assertTrue(text.strip())
        self.assertTrue(structured.answer.strip())


@unittest.skipUnless(
    os.environ.get("RUN_MODEL_API_TESTS") == "1",
    "set RUN_MODEL_API_TESTS=1 to run live model API tests",
)
class SubQLiveIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = SubQClient(SubQSettings.from_env())
        cls.addClassCleanup(cls.client.close)

    def test_binary_question_returns_bounded_probability(self):
        response = self.client.evaluate(
            "The deployment passed every required invariant check.",
            {
                "safe": BinaryQuestion(
                    instructions="Decide whether all required checks passed.",
                    criteria=BinaryCriteria(
                        true="Every required check passed.",
                        false="One or more required checks failed.",
                    ),
                )
            },
        )

        probability = response.answers["safe"].probability_yes
        self.assertIsInstance(probability, float)
        self.assertGreaterEqual(probability, 0.0)
        self.assertLessEqual(probability, 1.0)


if __name__ == "__main__":
    unittest.main()
