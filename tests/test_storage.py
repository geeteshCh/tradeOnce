import os
import unittest
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch

from bson import BSON
from bson.codec_options import CodecOptions
from pydantic import ValidationError

from storage import BuilderCandidate, MongoSettings, VerificationResult


class MongoSettingsTests(unittest.TestCase):
    def test_from_env_reads_values_and_masks_uri(self):
        env = {
            "MONGODB_URI": "mongodb+srv://example.invalid/test",
            "MONGODB_DATABASE": "test_database",
        }

        with patch.dict(os.environ, env, clear=True):
            settings = MongoSettings.from_env()

        self.assertEqual(settings.uri.get_secret_value(), env["MONGODB_URI"])
        self.assertEqual(settings.database, "test_database")
        self.assertNotIn(env["MONGODB_URI"], repr(settings))
        self.assertIn("**********", repr(settings))

    def test_from_env_uses_default_database(self):
        with patch.dict(
            os.environ,
            {"MONGODB_URI": "mongodb://localhost:27017"},
            clear=True,
        ):
            settings = MongoSettings.from_env()

        self.assertEqual(settings.database, "tradeonce")

    def test_from_env_requires_uri(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValidationError):
            MongoSettings.from_env()


class StorageModelTests(unittest.TestCase):
    codec_options = CodecOptions(tz_aware=True, tzinfo=UTC)

    def test_candidate_alias_and_bson_round_trip(self):
        local_time = datetime(
            2026,
            11,
            7,
            9,
            30,
            12,
            987654,
            tzinfo=timezone(timedelta(hours=-7)),
        )
        candidate = BuilderCandidate(
            _id="candidate-1",
            created_at=local_time,
            run_id="run-1",
            round=2,
            candidate="candidate_a",
            git_ref="refs/heads/candidate-1",
            summary="Initial candidate",
        )

        document = candidate.model_dump(by_alias=True)
        decoded = BSON.encode(document).decode(codec_options=self.codec_options)
        restored = BuilderCandidate.model_validate(decoded)

        self.assertEqual(candidate.id, "candidate-1")
        self.assertIn("_id", document)
        self.assertNotIn("id", document)
        self.assertEqual(
            candidate.created_at,
            datetime(2026, 11, 7, 16, 30, 12, 987000, tzinfo=UTC),
        )
        self.assertEqual(restored, candidate)

    def test_models_accept_field_name_or_mongo_alias_for_id(self):
        values = {
            "run_id": "run-1",
            "round": 0,
            "candidate": "candidate_a",
            "git_ref": "candidate-ref",
        }

        self.assertEqual(BuilderCandidate(id="field-id", **values).id, "field-id")
        self.assertEqual(BuilderCandidate(_id="alias-id", **values).id, "alias-id")

    def test_default_record_ids_are_unique(self):
        values = {
            "run_id": "run-1",
            "round": 0,
            "candidate": "candidate_a",
            "git_ref": "candidate-ref",
        }

        self.assertNotEqual(
            BuilderCandidate(**values).id, BuilderCandidate(**values).id
        )

    def test_verification_bson_round_trip_and_independent_list_defaults(self):
        first = VerificationResult(
            _id="verdict-1",
            candidate_id="candidate-1",
            decision="promote",
            hidden_pass_rate=0.95,
        )
        second = VerificationResult(
            _id="verdict-2",
            candidate_id="candidate-1",
            decision="reject",
            hidden_pass_rate=0.5,
        )

        first.invariant_failures.append("cash conservation")
        first.reasons.append("hidden tests passed")
        decoded = BSON.encode(first.model_dump(by_alias=True)).decode(
            codec_options=self.codec_options
        )

        self.assertEqual(VerificationResult.model_validate(decoded), first)
        self.assertEqual(second.invariant_failures, [])
        self.assertEqual(second.reasons, [])

    def test_candidate_rejects_invalid_critical_fields(self):
        valid = {
            "run_id": "run-1",
            "round": 0,
            "candidate": "candidate_a",
            "git_ref": "candidate-ref",
        }
        invalid_overrides = (
            {"run_id": ""},
            {"round": -1},
            {"candidate": ""},
            {"git_ref": ""},
            {"created_at": datetime(2026, 1, 1)},
            {"unexpected": "value"},
        )

        for override in invalid_overrides:
            with self.subTest(override=override), self.assertRaises(ValidationError):
                BuilderCandidate.model_validate(valid | override)

    def test_verification_rejects_invalid_critical_fields(self):
        valid = {
            "candidate_id": "candidate-1",
            "decision": "promote",
            "hidden_pass_rate": 0.5,
        }
        invalid_overrides = (
            {"candidate_id": ""},
            {"decision": "maybe"},
            {"hidden_pass_rate": -0.01},
            {"hidden_pass_rate": 1.01},
            {"invariant_failures": [1]},
            {"reasons": "not-a-list"},
        )

        for override in invalid_overrides:
            with self.subTest(override=override), self.assertRaises(ValidationError):
                VerificationResult.model_validate(valid | override)

    def test_assignment_validates_critical_fields(self):
        candidate = BuilderCandidate(
            run_id="run-1",
            round=0,
            candidate="candidate_a",
            git_ref="candidate-ref",
        )
        result = VerificationResult(
            candidate_id=candidate.id,
            decision="promote",
            hidden_pass_rate=0.5,
        )

        with self.assertRaises(ValidationError):
            candidate.round = -1
        with self.assertRaises(ValidationError):
            result.decision = "maybe"
        with self.assertRaises(ValidationError):
            result.hidden_pass_rate = 1.01


if __name__ == "__main__":
    unittest.main()
