import unittest
from unittest.mock import MagicMock

from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from storage import BuilderCandidate, HarnessMemory, MongoStore, VerificationResult
from storage.client import BUILDER_CANDIDATES, HARNESS_MEMORY, VERIFICATION_RESULTS


class HarnessMemoryModelTests(unittest.TestCase):
    def test_memory_requires_scope_revision_and_harness_files(self):
        valid = {
            "_id": "strategy-a",
            "scope": "strategy-a",
            "revision": 1,
            "candidate_id": "candidate-1",
            "verification_id": "verification-1",
            "git_ref": "refs/harness/candidate-1",
            "harness_files": {"harness.py": "def build():\n    return 1\n"},
        }

        memory = HarnessMemory.model_validate(valid)

        self.assertEqual(memory.library_files, {})
        for override in ({"scope": ""}, {"revision": 0}, {"harness_files": None}):
            with self.subTest(override=override), self.assertRaises(ValidationError):
                HarnessMemory.model_validate(valid | override)

    def test_file_defaults_are_independent(self):
        values = {
            "scope": "strategy-a",
            "revision": 1,
            "candidate_id": "candidate-1",
            "verification_id": "verification-1",
            "git_ref": "refs/harness/candidate-1",
            "harness_files": {},
        }
        first = HarnessMemory(_id="strategy-a", **values)
        second = HarnessMemory(_id="strategy-b", **(values | {"scope": "strategy-b"}))
        candidate = BuilderCandidate(
            run_id="run-1",
            round=0,
            candidate="candidate_a",
            git_ref="refs/harness/candidate-1",
        )

        first.library_files["Library/tool.py"] = "VALUE = 1\n"
        candidate.harness_files["harness.py"] = "VALUE = 2\n"

        self.assertEqual(second.library_files, {})
        self.assertEqual(
            BuilderCandidate(
                run_id="run-1",
                round=1,
                candidate="candidate_b",
                git_ref="refs/harness/candidate-2",
            ).harness_files,
            {},
        )


class HarnessMemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.database = MagicMock()
        self.candidates = MagicMock()
        self.verifications = MagicMock()
        self.memories = MagicMock()
        self.database.__getitem__.side_effect = {
            BUILDER_CANDIDATES: self.candidates,
            VERIFICATION_RESULTS: self.verifications,
            HARNESS_MEMORY: self.memories,
        }.__getitem__
        self.store = MongoStore.__new__(MongoStore)
        self.store._database = self.database

        self.candidate = BuilderCandidate(
            _id="candidate-1",
            run_id="run-1",
            round=0,
            candidate="candidate_a",
            git_ref="refs/harness/candidate-1",
            harness_files={"harness.py": "APPROVED = True\n"},
        )
        self.verification = VerificationResult(
            _id="verification-1",
            candidate_id=self.candidate.id,
            decision="promote",
            hidden_pass_rate=1,
        )
        self.candidates.find_one.return_value = self.candidate.model_dump(by_alias=True)
        self.verifications.find_one.return_value = self.verification.model_dump(
            by_alias=True
        )

    def test_initialize_creates_candidate_lookup_index(self):
        self.store.initialize()

        self.memories.create_index.assert_called_once_with(
            [("candidate_id", 1)],
            name="memory_by_candidate",
        )

    def memory(self, **overrides) -> HarnessMemory:
        values = {
            "_id": "strategy-a",
            "scope": "strategy-a",
            "revision": 1,
            "candidate_id": self.candidate.id,
            "verification_id": self.verification.id,
            "git_ref": self.candidate.git_ref,
            "harness_files": dict(self.candidate.harness_files),
            "library_files": {"Library/risk.py": "LIMIT = 10\n"},
        }
        return HarnessMemory.model_validate(values | overrides)

    def test_approved_memory_is_inserted_and_reloaded(self):
        memory = self.memory()
        self.memories.find_one.return_value = memory.model_dump(by_alias=True)

        self.store.save_harness_memory(memory)

        self.memories.insert_one.assert_called_once_with(
            memory.model_dump(by_alias=True)
        )
        self.assertEqual(self.store.get_harness_memory(memory.scope), memory)

    def test_only_perfect_promotion_verdict_can_write_memory(self):
        cases = (
            ({"decision": "reject"}, "did not promote"),
            ({"invariant_failures": ["cash conservation"]}, "invariant failures"),
            ({"hidden_pass_rate": 0.99}, "did not pass all hidden tests"),
        )

        for update, message in cases:
            with self.subTest(update=update):
                self.verifications.find_one.return_value = self.verification.model_copy(
                    update=update
                ).model_dump(by_alias=True)
                with self.assertRaisesRegex(ValueError, message):
                    self.store.save_harness_memory(self.memory())

        self.memories.insert_one.assert_not_called()
        self.memories.replace_one.assert_not_called()

    def test_initial_insert_propagates_duplicate_key(self):
        self.memories.insert_one.side_effect = DuplicateKeyError("duplicate scope")

        with self.assertRaises(DuplicateKeyError):
            self.store.save_harness_memory(self.memory())

    def test_missing_or_mismatched_proof_does_not_write_memory(self):
        cases = (
            (self.candidates, None, "candidate does not exist"),
            (
                self.candidates,
                self.candidate.model_copy(update={"git_ref": "refs/other"}).model_dump(
                    by_alias=True
                ),
                "git ref does not match",
            ),
            (self.verifications, None, "verification does not exist"),
            (
                self.verifications,
                self.verification.model_copy(
                    update={"candidate_id": "candidate-other"}
                ).model_dump(by_alias=True),
                "verification does not match",
            ),
        )

        for collection, document, message in cases:
            with self.subTest(message=message):
                self.candidates.find_one.return_value = self.candidate.model_dump(
                    by_alias=True
                )
                self.verifications.find_one.return_value = self.verification.model_dump(
                    by_alias=True
                )
                collection.find_one.return_value = document
                with self.assertRaisesRegex(ValueError, message):
                    self.store.save_harness_memory(self.memory())

        self.memories.insert_one.assert_not_called()
        self.memories.replace_one.assert_not_called()

    def test_tampered_harness_files_cannot_reuse_valid_proof(self):
        memory = self.memory(harness_files={"harness.py": "APPROVED = False\n"})

        with self.assertRaisesRegex(ValueError, "harness files do not match"):
            self.store.save_harness_memory(memory)

        self.memories.insert_one.assert_not_called()
        self.verifications.find_one.assert_not_called()

    def test_stale_revision_raises_without_upsert(self):
        memory = self.memory(revision=2)
        self.memories.replace_one.return_value.matched_count = 0

        with self.assertRaisesRegex(ValueError, "^stale memory revision$"):
            self.store.save_harness_memory(memory, expected_revision=1)

        self.memories.replace_one.assert_called_once_with(
            {"_id": memory.scope, "revision": 1},
            memory.model_dump(by_alias=True),
            upsert=False,
        )

    def test_current_revision_is_atomically_replaced(self):
        memory = self.memory(revision=2)
        self.memories.replace_one.return_value.matched_count = 1

        self.store.save_harness_memory(memory, expected_revision=1)

        self.memories.replace_one.assert_called_once_with(
            {"_id": memory.scope, "revision": 1},
            memory.model_dump(by_alias=True),
            upsert=False,
        )

    def test_identity_and_revision_are_validated_before_proof_lookup(self):
        with self.assertRaisesRegex(ValueError, "id must match scope"):
            self.store.save_harness_memory(self.memory(_id="wrong-scope"))
        with self.assertRaisesRegex(ValueError, "revision must follow"):
            self.store.save_harness_memory(self.memory(revision=2))

        self.candidates.find_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
