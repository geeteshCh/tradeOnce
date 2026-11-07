import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from harness2.core.proposer import ProposerError
from pydantic import BaseModel

from harnessing import proposer as proposer_module
from harnessing.proposer import GeminiHarnessProposer
from providers.gemini import GeminiClient


class GeminiHarnessProposerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MagicMock(spec=GeminiClient)
        self.proposer = GeminiHarnessProposer(self.client, "gemini-test")

    def test_structured_schemas_avoid_unsupported_additional_properties(self) -> None:
        for model in (
            proposer_module._HarnessEdits,
            proposer_module._Selection,
            proposer_module._Experience,
        ):
            with self.subTest(model=model.__name__):
                schema = json.dumps(model.model_json_schema())
                self.assertNotIn("additionalProperties", schema)

    def test_harvest_writes_only_supplied_fixed_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            harness = root / "harness"
            harness.mkdir()
            (harness / "systemprompt.md").write_text("old system", encoding="utf-8")
            (harness / "guardrails.md").write_text("keep guardrails", encoding="utf-8")
            (harness / "custom.md").write_text("keep custom", encoding="utf-8")

            def structured(
                _prompt: str, response_model: type[BaseModel], **_kwargs: object
            ) -> BaseModel:
                return response_model.model_validate(
                    {
                        "system_prompt": "new system",
                        "notes": "updated one component",
                        "arbitrary_filename": "must be ignored",
                    }
                )

            self.client.generate_structured.side_effect = structured

            reply = self.proposer.call(
                "Improve the harness",
                root,
                "candidate.1",
                harvest="context/harness",
            )

            self.assertEqual((harness / "systemprompt.md").read_text(), "new system")
            self.assertEqual((harness / "guardrails.md").read_text(), "keep guardrails")
            self.assertEqual((harness / "custom.md").read_text(), "keep custom")
            self.assertFalse((harness / "arbitrary_filename").exists())
            self.assertEqual(reply.workspace, harness)
            self.assertEqual(reply.text, "<notes>updated one component</notes>")

    def test_selection_and_experience_use_harness2_wire_format(self) -> None:
        self.client.generate_structured.side_effect = (
            lambda _prompt, response_model, **_kwargs: (
                response_model(chosen_id="A-1", why="best <evidence>")
                if response_model is proposer_module._Selection
                else response_model(
                    cue="unknown order status",
                    lesson="reconcile before retry",
                    evidence="candidate passed hidden cases",
                    confidence="high",
                    edits="component: guardrails.md",
                )
            )
        )

        selection = self.proposer.call("Choose", None, "select.agg1")
        experience = self.proposer.call("Reflect", None, "experience.agg1")

        self.assertEqual(
            selection.text,
            "<why>best &lt;evidence&gt;</why>\n```choice\nA-1\n```",
        )
        self.assertEqual(
            experience.text,
            "<edits>\ncomponent: guardrails.md\n</edits>\n\n"
            "<experience>\n"
            "cue: unknown order status\n"
            "lesson: reconcile before retry\n"
            "evidence: candidate passed hidden cases\n"
            "confidence: high\n"
            "</experience>",
        )

    def test_context_excludes_environment_files_and_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            (root / "visible.md").write_text("public evidence", encoding="utf-8")
            (root / ".env").write_text("SECRET=hidden", encoding="utf-8")
            self.client.generate_text.return_value = "ok"

            self.proposer.call("Inspect", root, "plain")

            prompt = self.client.generate_text.call_args.args[0]
            self.assertIn("public evidence", prompt)
            self.assertNotIn("SECRET=hidden", prompt)

            with tempfile.TemporaryDirectory() as outside_dir:
                secret = Path(outside_dir) / "secret.txt"
                secret.write_text("outside", encoding="utf-8")
                (root / "escape").symlink_to(secret)
                with self.assertRaises(ProposerError) as raised:
                    self.proposer.call("Inspect", root, "plain")

            self.assertEqual(raised.exception.attempts, 1)

    def test_client_errors_become_single_attempt_proposer_errors(self) -> None:
        provider_error = TypeError("Gemini returned no parsed response")
        self.client.generate_text.side_effect = provider_error

        with self.assertRaises(ProposerError) as raised:
            self.proposer.call("Generate", None, "plain")

        self.assertIs(raised.exception.__cause__, provider_error)
        self.assertEqual(raised.exception.attempts, 1)
        self.assertEqual(len(self.proposer._calls), 1)
        self.assertFalse(self.proposer._calls[0].ok)
        self.client.generate_text.assert_called_once()


if __name__ == "__main__":
    unittest.main()
