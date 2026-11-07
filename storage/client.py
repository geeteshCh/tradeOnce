"""Small synchronous MongoDB client shared by the Builder, Judge, and loop."""

from types import TracebackType
from typing import Any, Self

from pymongo import ASCENDING, MongoClient
from pymongo.database import Database

from storage.config import MongoSettings
from storage.models import BuilderCandidate, HarnessMemory, VerificationResult

BUILDER_CANDIDATES = "builder_candidates"
VERIFICATION_RESULTS = "verification_results"
HARNESS_MEMORY = "harness_memory"


class MongoStore:
    """Own one reusable PyMongo connection; use as a context manager or close it."""

    def __init__(self, settings: MongoSettings) -> None:
        self._client: MongoClient[dict[str, Any]] = MongoClient(
            settings.uri.get_secret_value(),
            tz_aware=True,
            serverSelectionTimeoutMS=5000,
        )
        self._database: Database[dict[str, Any]] = self._client[settings.database]

    @classmethod
    def from_env(cls) -> Self:
        """Open a store using the environment populated by .envrc or the caller."""
        return cls(MongoSettings.from_env())

    def initialize(self) -> None:
        """Create the collections and indexes for their common read paths."""
        self._database[BUILDER_CANDIDATES].create_index(
            [("run_id", ASCENDING), ("round", ASCENDING), ("_id", ASCENDING)],
            name="candidates_by_run_round",
        )
        self._database[VERIFICATION_RESULTS].create_index(
            [
                ("candidate_id", ASCENDING),
                ("created_at", ASCENDING),
                ("_id", ASCENDING),
            ],
            name="verifications_by_candidate_time",
        )
        self._database[HARNESS_MEMORY].create_index(
            [("candidate_id", ASCENDING)],
            name="memory_by_candidate",
        )

    def ping(self) -> bool:
        """Check connectivity and authentication; driver errors propagate."""
        return bool(self._client.admin.command("ping")["ok"] == 1)

    def save_candidate(self, candidate: BuilderCandidate) -> str:
        """Insert a candidate, or replace its document when the same ID is saved."""
        self._database[BUILDER_CANDIDATES].replace_one(
            {"_id": candidate.id}, candidate.model_dump(by_alias=True), upsert=True
        )
        return candidate.id

    def get_candidate(self, candidate_id: str) -> BuilderCandidate | None:
        """Read and validate one candidate; return None when its ID is absent."""
        document = self._database[BUILDER_CANDIDATES].find_one({"_id": candidate_id})
        return (
            BuilderCandidate.model_validate(document) if document is not None else None
        )

    def list_candidates(self, run_id: str) -> list[BuilderCandidate]:
        """Read a run's candidates in round order, with a stable ID tiebreaker."""
        documents = (
            self._database[BUILDER_CANDIDATES]
            .find({"run_id": run_id})
            .sort([("round", ASCENDING), ("_id", ASCENDING)])
        )
        return [BuilderCandidate.model_validate(document) for document in documents]

    def save_verification(self, result: VerificationResult) -> str:
        """Insert a verdict, or replace it when the same result ID is saved."""
        self._database[VERIFICATION_RESULTS].replace_one(
            {"_id": result.id}, result.model_dump(by_alias=True), upsert=True
        )
        return result.id

    def get_verification(self, result_id: str) -> VerificationResult | None:
        """Read and validate one verification result, or return None if absent."""
        document = self._database[VERIFICATION_RESULTS].find_one({"_id": result_id})
        return (
            VerificationResult.model_validate(document)
            if document is not None
            else None
        )

    def list_verifications(self, candidate_id: str) -> list[VerificationResult]:
        """Read a candidate's verdict history in creation order."""
        documents = (
            self._database[VERIFICATION_RESULTS]
            .find({"candidate_id": candidate_id})
            .sort([("created_at", ASCENDING), ("_id", ASCENDING)])
        )
        return [VerificationResult.model_validate(document) for document in documents]

    def get_harness_memory(self, scope: str) -> HarnessMemory | None:
        """Read and validate the latest approved harness snapshot for a scope."""
        document = self._database[HARNESS_MEMORY].find_one({"_id": scope})
        return HarnessMemory.model_validate(document) if document is not None else None

    def save_harness_memory(
        self, memory: HarnessMemory, *, expected_revision: int = 0
    ) -> None:
        """Commit approved memory using an atomic revision compare-and-swap.

        The referenced candidate and held-out verification must already exist. Only
        a perfect promotion verdict can advance memory, and concurrent or stale
        writers cannot overwrite the current revision.
        """
        if memory.id != memory.scope:
            raise ValueError("memory id must match scope")
        if memory.revision != expected_revision + 1:
            raise ValueError("memory revision must follow expected revision")

        candidate = self.get_candidate(memory.candidate_id)
        if candidate is None:
            raise ValueError("memory candidate does not exist")
        if candidate.git_ref != memory.git_ref:
            raise ValueError("memory git ref does not match candidate")
        if candidate.harness_files != memory.harness_files:
            raise ValueError("memory harness files do not match candidate")
        if candidate.source_files != memory.source_files:
            raise ValueError("memory source files do not match candidate")

        verification = self.get_verification(memory.verification_id)
        if verification is None:
            raise ValueError("memory verification does not exist")
        if verification.candidate_id != memory.candidate_id:
            raise ValueError("memory verification does not match candidate")
        if verification.decision != "promote":
            raise ValueError("memory verification did not promote candidate")
        if verification.invariant_failures:
            raise ValueError("memory verification has invariant failures")
        if verification.hidden_pass_rate != 1:
            raise ValueError("memory verification did not pass all hidden tests")

        document = memory.model_dump(by_alias=True)
        collection = self._database[HARNESS_MEMORY]
        if expected_revision == 0:
            collection.insert_one(document)
            return

        result = collection.replace_one(
            {"_id": memory.scope, "revision": expected_revision},
            document,
            upsert=False,
        )
        if result.matched_count == 0:
            raise ValueError("stale memory revision")

    def close(self) -> None:
        """Release connection pools when this store is no longer needed."""
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
