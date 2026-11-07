import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from pymongo import MongoClient

from harnessing.demo import DEFAULT_HARNESS, demo_probe, make_demo_verifier
from harnessing.runner import HarnessRunner, HarnessSettings
from storage import MongoSettings, MongoStore
from storage.client import BUILDER_CANDIDATES, HARNESS_MEMORY, VERIFICATION_RESULTS
from tests.test_harness_runner import ScriptedProposer, retry_task


@unittest.skipUnless(
    os.environ.get("RUN_MONGODB_TESTS") == "1",
    "set RUN_MONGODB_TESTS=1 to run MongoDB integration tests",
)
class HarnessRunnerMongoIntegrationTests(unittest.TestCase):
    """Exercise real Harness2 scheduling across a fresh MongoStore connection."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = MongoSettings.from_env()
        cls.raw_client = MongoClient(
            cls.settings.uri.get_secret_value(),
            tz_aware=True,
            serverSelectionTimeoutMS=5000,
        )
        cls.addClassCleanup(cls.raw_client.close)
        cls.database = cls.raw_client[cls.settings.database]

    def setUp(self) -> None:
        self.prefix = f"harness-runner-{uuid4().hex}"
        self.scope = f"{self.prefix}-scope"
        self.git_ref = f"refs/test/{self.prefix}"
        self.candidate_ids: set[str] = set()
        self.verification_ids: set[str] = set()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.runs_dir = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.database[HARNESS_MEMORY].delete_one({"_id": self.scope})
        if self.verification_ids:
            self.database[VERIFICATION_RESULTS].delete_many(
                {"_id": {"$in": list(self.verification_ids)}}
            )
        if self.candidate_ids:
            self.database[BUILDER_CANDIDATES].delete_many(
                {"_id": {"$in": list(self.candidate_ids)}}
            )

    def runner(self, store: MongoStore, proposer: ScriptedProposer) -> HarnessRunner:
        return HarnessRunner(
            store,
            proposer,
            HarnessSettings(
                scope=self.scope,
                git_ref=self.git_ref,
                runs_dir=self.runs_dir,
                k=1,
                mode="sequential",
            ),
        )

    def remember_result_ids(self, result) -> None:
        """Track only records created by this test for exact cleanup."""
        self.candidate_ids.add(result.candidate_id)
        self.verification_ids.add(result.verification_id)

    def test_verified_memory_reloads_in_fresh_store_and_rejects_no_change(self) -> None:
        verifier = make_demo_verifier(seed=404)
        first_store = MongoStore(self.settings)
        first_store.initialize()
        try:
            first = self.runner(first_store, ScriptedProposer()).run(
                retry_task(f"{self.prefix}-first"),
                initial_harness=dict(DEFAULT_HARNESS),
                probe=demo_probe,
                verifier=verifier,
            )
            self.remember_result_ids(first)
            persisted = first_store.get_harness_memory(self.scope)
        finally:
            first_store.close()

        self.assertEqual(first.decision, "promote")
        self.assertEqual(first.memory_revision, 1)
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertTrue(persisted.library_files)

        second_store = MongoStore(self.settings)
        try:
            proposer = ScriptedProposer()
            second = self.runner(second_store, proposer).run(
                retry_task(f"{self.prefix}-second"),
                initial_harness=dict(DEFAULT_HARNESS),
                probe=demo_probe,
                verifier=verifier,
            )
            self.remember_result_ids(second)
            reloaded = second_store.get_harness_memory(self.scope)
        finally:
            second_store.close()

        self.assertEqual(second.decision, "reject")
        self.assertEqual(second.loaded_memory_revision, 1)
        self.assertEqual(second.memory_revision, 1)
        self.assertEqual(reloaded, persisted)
        restored_context = "\n".join(
            content
            for snapshot in proposer.context_history
            for content in snapshot.values()
        )
        self.assertIn("reconcile broker ledger before retry", restored_context)


if __name__ == "__main__":
    unittest.main()
