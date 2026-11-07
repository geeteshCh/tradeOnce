import os
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import ValidationError
from pymongo import MongoClient

from storage import BuilderCandidate, MongoSettings, MongoStore, VerificationResult
from storage.client import BUILDER_CANDIDATES, VERIFICATION_RESULTS


@unittest.skipUnless(
    os.environ.get("RUN_MONGODB_TESTS") == "1",
    "set RUN_MONGODB_TESTS=1 to run MongoDB integration tests",
)
class MongoStoreIntegrationTests(unittest.TestCase):
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
        self.prefix = f"tradeonce-integration-{uuid4().hex}"
        self.candidate_ids: set[str] = set()
        self.verification_ids: set[str] = set()

    def tearDown(self):
        if self.candidate_ids:
            self.database[BUILDER_CANDIDATES].delete_many(
                {"_id": {"$in": list(self.candidate_ids)}}
            )
        if self.verification_ids:
            self.database[VERIFICATION_RESULTS].delete_many(
                {"_id": {"$in": list(self.verification_ids)}}
            )

    def candidate(self, suffix: str, **overrides) -> BuilderCandidate:
        candidate_id = f"{self.prefix}-candidate-{suffix}"
        self.candidate_ids.add(candidate_id)
        values = {
            "_id": candidate_id,
            "run_id": f"{self.prefix}-run",
            "round": 0,
            "candidate": f"candidate_{suffix}",
            "git_ref": f"refs/test/{self.prefix}/{suffix}",
            "summary": f"candidate {suffix}",
        }
        return BuilderCandidate.model_validate(values | overrides)

    def verification(self, suffix: str, **overrides) -> VerificationResult:
        result_id = f"{self.prefix}-verification-{suffix}"
        self.verification_ids.add(result_id)
        values = {
            "_id": result_id,
            "candidate_id": f"{self.prefix}-candidate-main",
            "decision": "promote",
            "hidden_pass_rate": 1.0,
        }
        return VerificationResult.model_validate(values | overrides)

    def test_candidate_read_replace_filter_order_and_missing(self):
        later = self.candidate("later", round=2)
        first_b = self.candidate("b", round=1)
        first_a = self.candidate("a", round=1)
        other_run = self.candidate("other", run_id=f"{self.prefix}-other-run")

        for candidate in (later, first_b, first_a, other_run):
            self.assertEqual(self.store.save_candidate(candidate), candidate.id)

        replacement = first_a.model_copy(update={"summary": "replacement"})
        self.assertEqual(self.store.save_candidate(replacement), first_a.id)

        self.assertEqual(self.store.get_candidate(first_a.id), replacement)
        self.assertEqual(
            [candidate.id for candidate in self.store.list_candidates(first_a.run_id)],
            [first_a.id, first_b.id, later.id],
        )
        self.assertEqual(
            self.database[BUILDER_CANDIDATES].count_documents({"_id": first_a.id}),
            1,
        )
        self.assertIsNone(self.store.get_candidate(f"{self.prefix}-missing"))

    def test_verification_read_replace_filter_order_and_history(self):
        candidate = self.candidate("main")
        self.store.save_candidate(candidate)
        base_time = datetime(2026, 1, 1, tzinfo=UTC)
        later = self.verification("later", created_at=base_time + timedelta(seconds=1))
        first_b = self.verification(
            "b",
            created_at=base_time,
            decision="reject",
            hidden_pass_rate=0.4,
            reasons=["hidden failure"],
        )
        first_a = self.verification("a", created_at=base_time)
        other_candidate = self.verification(
            "other",
            candidate_id=f"{self.prefix}-candidate-other",
        )

        for result in (later, first_b, first_a, other_candidate):
            self.assertEqual(self.store.save_verification(result), result.id)

        replacement = first_b.model_copy(
            update={"hidden_pass_rate": 0.6, "reasons": ["rechecked"]}
        )
        self.assertEqual(self.store.save_verification(replacement), first_b.id)

        self.assertEqual(self.store.get_verification(first_b.id), replacement)
        self.assertEqual(
            [result.id for result in self.store.list_verifications(candidate.id)],
            [first_a.id, first_b.id, later.id],
        )
        self.assertEqual(
            self.database[VERIFICATION_RESULTS].count_documents({"_id": first_b.id}),
            1,
        )
        self.assertEqual(
            self.database[VERIFICATION_RESULTS].count_documents(
                {"candidate_id": candidate.id}
            ),
            3,
        )
        self.assertIsNone(self.store.get_verification(f"{self.prefix}-missing"))

    def test_invalid_database_document_is_rejected_on_read(self):
        invalid_id = f"{self.prefix}-candidate-invalid"
        self.candidate_ids.add(invalid_id)
        self.database[BUILDER_CANDIDATES].insert_one(
            {
                "_id": invalid_id,
                "created_at": datetime.now(UTC),
                "run_id": f"{self.prefix}-run",
                "round": -1,
                "candidate": "candidate_invalid",
                "git_ref": "refs/test/invalid",
                "summary": "",
            }
        )

        with self.assertRaises(ValidationError):
            self.store.get_candidate(invalid_id)


if __name__ == "__main__":
    unittest.main()
