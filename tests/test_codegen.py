import os
import unittest
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from builder.codegen import CodePatch, generate_patch
from providers.gemini import GeminiClient

SOURCE_FILES = {
    "executor/run.py": "def execute(intent):\n    return intent\n",
    "executor/LESSONS.md": "# Lessons\nReconcile unknown outcomes.\n",
}
HARNESS_FILES = {
    "systemprompt.md": "Keep changes minimal.",
    "guardrails.md": "Never blindly retry an unknown order.",
    "memory.md": "Accepted timeouts require reconciliation.",
    "toolguidance.md": "Use the broker lookup endpoint.",
}


class CodePatchTests(unittest.TestCase):
    def test_requires_nonblank_complete_outputs(self) -> None:
        for field in ("run_source", "lessons", "summary"):
            values = {
                "run_source": "print('safe')",
                "lessons": "reconcile first",
                "summary": "added reconciliation",
                field: "   ",
            }
            with self.subTest(field=field), self.assertRaises(ValidationError):
                CodePatch(**values)

    def test_schema_is_compatible_with_gemini_structured_output(self) -> None:
        self.assertNotIn("additionalProperties", CodePatch.model_json_schema())


class GeneratePatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MagicMock(spec=GeminiClient)
        self.expected = CodePatch(
            run_source="print('reconciled')",
            lessons="Reconcile unknown submissions before retrying.",
            summary="Added order reconciliation.",
        )
        self.client.generate_structured.return_value = self.expected

    def test_sends_only_supplied_public_inputs_in_one_typed_call(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": "ambient-secret"}):
            result = generate_patch(
                self.client,
                source_files=SOURCE_FILES,
                harness_files=HARNESS_FILES,
                feedback="Training t02 produced a duplicate fill.",
            )

        self.assertEqual(result, self.expected)
        self.client.generate_structured.assert_called_once()
        args = self.client.generate_structured.call_args.args
        kwargs = self.client.generate_structured.call_args.kwargs
        prompt = args[0]
        self.assertIs(args[1], CodePatch)
        for path, content in (*SOURCE_FILES.items(), *HARNESS_FILES.items()):
            self.assertIn(path, prompt)
            self.assertIn(content.strip(), prompt)
        self.assertIn("Training t02 produced a duplicate fill.", prompt)
        self.assertNotIn("ambient-secret", prompt)
        self.assertEqual(kwargs["max_output_tokens"], 8192)
        self.assertIn("complete replacement contents", kwargs["system_instruction"])

    def test_rejects_files_outside_fixed_public_surface_before_call(self) -> None:
        unsafe_sources = {**SOURCE_FILES, "auditor/hidden.py": "SECRET = True"}

        with self.assertRaises(ValueError):
            generate_patch(
                self.client,
                source_files=unsafe_sources,
                harness_files=HARNESS_FILES,
                feedback="public failure",
            )

        self.client.generate_structured.assert_not_called()

    def test_rejects_incomplete_context_and_blank_feedback(self) -> None:
        cases = (
            (
                {"executor/run.py": SOURCE_FILES["executor/run.py"]},
                HARNESS_FILES,
                "feedback",
            ),
            (SOURCE_FILES, {"memory.md": "lesson"}, "feedback"),
            (SOURCE_FILES, HARNESS_FILES, "   "),
        )
        for source_files, harness_files, feedback in cases:
            with self.subTest(feedback=feedback), self.assertRaises(ValueError):
                generate_patch(
                    self.client,
                    source_files=source_files,
                    harness_files=harness_files,
                    feedback=feedback,
                )

        self.client.generate_structured.assert_not_called()


if __name__ == "__main__":
    unittest.main()
