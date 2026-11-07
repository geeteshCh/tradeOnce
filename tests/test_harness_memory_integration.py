import os
import unittest
from uuid import uuid4

from pymongo import MongoClient

from storage import (
    BuilderCandidate,
    HarnessMemory,
    MongoSettings,
    MongoStore,
    VerificationResult,
)
from storage.client import BUILDER_CANDIDATES, HARNESS_MEMORY, VERIFICATION_RESULTS


@unittest.skipUnless(
    os.environ.get("RUN_MONGODB_TESTS") == "1",
    "set RUN_MONGODB_TESTS=1 to run MongoDB integration tests",
)
class HarnessMemoryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = MongoSettings.from_env()
        cls.store = MongoStore(cls.settings)
        cls.addClassCleanup(cls.store.close)
        cls.raw_client = MongoClient(
            cls.settings.uri.get_secret_value(),
            tz_aware=True,
            serverSelectionTimeoutMS=5000,
        )
        cls.addClassCleanup(cls.raw_client.close)
        cls.database = cls.raw_client[cls.settings.database]
        if not cls.store.ping():
            raise AssertionError("MongoDB ping did not return ok=1")
        cls.store.initialize()

    def setUp(self):
        self.prefix = f"harness-memory-{uuid4().hex}"
        self.scope = f"{self.prefix}-scope"
        self.candidate_ids: set[str] = set()
        self.verification_ids: set[str] = set()

    def tearDown(self):
        self.database[HARNESS_MEMORY].delete_one({"_id": self.scope})
        if self.candidate_ids:
            self.database[BUILDER_CANDIDATES].delete_many(
                {"_id": {"$in": list(self.candidate_ids)}}
            )
        if self.verification_ids:
            self.database[VERIFICATION_RESULTS].delete_many(
                {"_id": {"$in": list(self.verification_ids)}}
            )

    def candidate(self, suffix: str = "main") -> BuilderCandidate:
        candidate_id = f"{self.prefix}-candidate-{suffix}"
        self.candidate_ids.add(candidate_id)
        return BuilderCandidate(
            _id=candidate_id,
            run_id=f"{self.prefix}-run",
            round=0,
            candidate=f"candidate_{suffix}",
            git_ref=f"refs/test/{self.prefix}/{suffix}",
            harness_files={"harness.py": f"CANDIDATE = {suffix!r}\n"},
        )

    def verification(
        self, candidate: BuilderCandidate, suffix: str = "main", **overrides
    ) -> VerificationResult:
        verification_id = f"{self.prefix}-verification-{suffix}"
        self.verification_ids.add(verification_id)
        values = {
            "_id": verification_id,
            "candidate_id": candidate.id,
            "decision": "promote",
            "hidden_pass_rate": 1.0,
        }
        return VerificationResult.model_validate(values | overrides)

    def memory(
        self,
        candidate: BuilderCandidate,
        verification: VerificationResult,
        revision: int = 1,
        **overrides,
    ) -> HarnessMemory:
        values = {
            "_id": self.scope,
            "scope": self.scope,
            "revision": revision,
            "candidate_id": candidate.id,
            "verification_id": verification.id,
            "git_ref": candidate.git_ref,
            "harness_files": dict(candidate.harness_files),
            "library_files": {"Library/shared.py": "APPROVED = True\n"},
        }
        return HarnessMemory.model_validate(values | overrides)

    def test_approved_memory_persists_reloads_and_stale_write_preserves_it(self):
        candidate = self.candidate()
        verification = self.verification(candidate)
        self.store.save_candidate(candidate)
        self.store.save_verification(verification)
        initial = self.memory(candidate, verification)

        self.store.save_harness_memory(initial)
        self.assertEqual(self.store.get_harness_memory(self.scope), initial)

        tampered = initial.model_copy(
            update={
                "revision": 2,
                "harness_files": {"toolguidance.md": '{"retry_policy":"blind_retry"}'},
            }
        )
        with self.assertRaisesRegex(ValueError, "harness files do not match"):
            self.store.save_harness_memory(tampered, expected_revision=1)

        stale_candidate = self.candidate("stale")
        stale_verification = self.verification(stale_candidate, "stale")
        self.store.save_candidate(stale_candidate)
        self.store.save_verification(stale_verification)
        stale = self.memory(stale_candidate, stale_verification, revision=3)
        with self.assertRaisesRegex(ValueError, "^stale memory revision$"):
            self.store.save_harness_memory(stale, expected_revision=2)

        self.assertEqual(self.store.get_harness_memory(self.scope), initial)

    def test_reject_and_invalid_proof_cannot_create_or_advance_memory(self):
        candidate = self.candidate()
        rejected = self.verification(candidate, decision="reject", hidden_pass_rate=1.0)
        self.store.save_candidate(candidate)
        self.store.save_verification(rejected)

        with self.assertRaisesRegex(ValueError, "did not promote"):
            self.store.save_harness_memory(self.memory(candidate, rejected))
        self.assertIsNone(self.store.get_harness_memory(self.scope))

        approved = self.verification(candidate, "approved")
        self.store.save_verification(approved)
        initial = self.memory(candidate, approved)
        self.store.save_harness_memory(initial)

        invalid_results = (
            self.verification(
                candidate,
                "invariants",
                invariant_failures=["cash conservation"],
            ),
            self.verification(candidate, "hidden", hidden_pass_rate=0.99),
        )
        for result in invalid_results:
            self.store.save_verification(result)
            with self.subTest(result=result.id), self.assertRaises(ValueError):
                self.store.save_harness_memory(
                    self.memory(candidate, result, revision=2), expected_revision=1
                )

        missing_candidate = self.candidate("missing")
        missing_verification = self.verification(missing_candidate, "missing")
        with self.assertRaisesRegex(ValueError, "candidate does not exist"):
            self.store.save_harness_memory(
                self.memory(missing_candidate, missing_verification, revision=2),
                expected_revision=1,
            )

        mismatched_candidate = self.candidate("mismatch")
        mismatched_verification = self.verification(candidate, "mismatch")
        self.store.save_candidate(mismatched_candidate)
        self.store.save_verification(mismatched_verification)
        with self.assertRaisesRegex(ValueError, "verification does not match"):
            self.store.save_harness_memory(
                self.memory(
                    mismatched_candidate,
                    mismatched_verification,
                    revision=2,
                    git_ref=mismatched_candidate.git_ref,
                ),
                expected_revision=1,
            )

        self.assertEqual(self.store.get_harness_memory(self.scope), initial)


if __name__ == "__main__":
    unittest.main()
