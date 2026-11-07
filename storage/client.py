"""Small synchronous MongoDB client shared by the Builder, Judge, and loop."""

from types import TracebackType
from typing import Any, Self

from pymongo import ASCENDING, MongoClient
from pymongo.database import Database

from storage.config import MongoSettings
from storage.models import BuilderCandidate, VerificationResult

BUILDER_CANDIDATES = "builder_candidates"
VERIFICATION_RESULTS = "verification_results"


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
        """Create the two collections and indexes for their common read paths."""
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
