from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import cleanup_eua_legacy_commemoratives as cleanup


class CleanupEuaLegacyCommemorativesTests(unittest.TestCase):
    def test_verify_exact_records_accepts_only_identical_records(self) -> None:
        records = [{"id": "one", "condition": "Tenho"}]

        cleanup.verify_exact_records(
            label="Coin", expected=records, actual=[dict(records[0])]
        )

        with self.assertRaisesRegex(cleanup.CleanupError, "mudou desde o backup"):
            cleanup.verify_exact_records(
                label="Coin",
                expected=records,
                actual=[{"id": "one", "condition": "Não Tenho"}],
            )

    def test_checkpoint_recovered_missing_is_idempotent(self) -> None:
        state = {
            "deleted_coin_variants": {},
            "deleted_coins": {},
        }

        cleanup.checkpoint_recovered_missing(
            state=state,
            missing_variants={"variant-1"},
            missing_coins={"coin-1"},
        )
        first = dict(state["deleted_coins"]["coin-1"])
        cleanup.checkpoint_recovered_missing(
            state=state,
            missing_variants={"variant-1"},
            missing_coins={"coin-1"},
        )

        self.assertEqual(state["deleted_coins"]["coin-1"], first)
        self.assertEqual(
            state["deleted_coin_variants"]["variant-1"]["result"],
            "recovered_missing",
        )

    def test_state_rejects_a_different_candidate_set(self) -> None:
        report = {
            "cleanup_candidates": {
                "coin_ids": ["coin"],
                "coin_variant_ids": ["variant"],
            }
        }
        backup = cleanup.PROJECT_ROOT / "backups" / "base44-test"
        artifact = cleanup.PROJECT_ROOT / "backups" / "migrations" / "artifact-test"
        state = cleanup.initial_state(
            backup=backup, artifact=artifact, report=report
        )
        state["candidate_fingerprint"] = "wrong"

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            cleanup.write_private_json_atomic(path, state)
            with self.assertRaisesRegex(cleanup.CleanupError, "candidate_fingerprint"):
                cleanup.load_or_initialize_state(
                    path=path,
                    backup=backup,
                    artifact=artifact,
                    report=report,
                )


if __name__ == "__main__":
    unittest.main()
