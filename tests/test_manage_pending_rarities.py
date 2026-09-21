from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.manage_pending_rarities import (
    apply_rarity_batch,
    build_rarity_batch,
    discover_pending_catalogues,
    write_country_outputs,
)


def pending_catalogue() -> dict[str, object]:
    return {
        "country": "País Teste",
        "periods": [
            {
                "title": "País Teste › República › 2000 - 2026",
                "ruler": "República",
                "startYear": 2000,
                "endYear": 2026,
                "coins": [
                    {
                        "denomination": "1 unidade",
                        "value": 1.0,
                        "unit": "unidade",
                        "issuePeriod": "2000",
                        "startYear": 2000,
                        "endYear": 2000,
                        "availability": "still needed to calculate",
                        "detailUrl": "https://example.test/same-coin",
                        "obverseImage": "",
                        "reverseImage": "",
                    },
                    {
                        "denomination": "2 unidades",
                        "value": 2.0,
                        "unit": "unidades",
                        "issuePeriod": "2001",
                        "startYear": 2001,
                        "endYear": 2001,
                        "availability": "still needed to calculate",
                        "detailUrl": "https://example.test/same-coin",
                        "obverseImage": "",
                        "reverseImage": "",
                    },
                ],
            }
        ],
    }


class PendingRarityBatchTests(unittest.TestCase):
    def create_pending(self, root: Path) -> Path:
        country_dir = root / "america" / "pais-teste"
        country_dir.mkdir(parents=True)
        path = country_dir / "app-catalog-pending.json"
        path.write_text(json.dumps(pending_catalogue()), encoding="utf-8")
        return path

    def test_batch_uses_position_ids_when_detail_urls_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.create_pending(root)

            entries = discover_pending_catalogues(root)
            batch = build_rarity_batch(entries)

            self.assertEqual(len(entries), 1)
            self.assertEqual(
                [coin["coinId"] for coin in batch["countries"][0]["coins"]],
                ["p1-c1", "p1-c2"],
            )

    def test_discovery_can_select_one_country_by_catalogue_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.create_pending(root)

            self.assertEqual(len(discover_pending_catalogues(root, "País Teste")), 1)
            self.assertEqual(discover_pending_catalogues(root, "Outro País"), [])

    def test_apply_updates_only_availability_and_writes_all_country_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pending_path = self.create_pending(root)
            source_before = pending_path.read_text(encoding="utf-8")
            entries = discover_pending_catalogues(root)
            final_batch = copy.deepcopy(build_rarity_batch(entries))
            final_batch["countries"][0]["coins"][0]["availability"] = "historical"
            final_batch["countries"][0]["coins"][1]["availability"] = "circulating"

            results = apply_rarity_batch(final_batch, entries)
            write_country_outputs(results)

            country_dir = pending_path.parent
            app_catalogue = json.loads((country_dir / "app-catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [coin["availability"] for coin in app_catalogue["periods"][0]["coins"]],
                ["historical", "circulating"],
            )
            self.assertEqual(pending_path.read_text(encoding="utf-8"), source_before)
            self.assertTrue((country_dir / "app-catalog-final.json").is_file())
            self.assertTrue((country_dir / "availability-statistics.json").is_file())
            self.assertTrue((country_dir / "coins-availability.xlsx").is_file())

    def test_cleanup_removes_intermediate_files_only_after_outputs_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pending_path = self.create_pending(root)
            country_dir = pending_path.parent
            (country_dir / "ucoin-catalog.json").write_text("{}", encoding="utf-8")
            (country_dir / "differences-pending.json").write_text("[]", encoding="utf-8")
            entries = discover_pending_catalogues(root)
            final_batch = build_rarity_batch(entries)
            for coin in final_batch["countries"][0]["coins"]:
                coin["availability"] = "historical"

            results = apply_rarity_batch(final_batch, entries)
            write_country_outputs(results, cleanup_intermediate=True)

            self.assertTrue((country_dir / "app-catalog.json").is_file())
            self.assertTrue((country_dir / "availability-statistics.json").is_file())
            self.assertTrue((country_dir / "coins-availability.xlsx").is_file())
            self.assertFalse((country_dir / "ucoin-catalog.json").exists())
            self.assertFalse((country_dir / "app-catalog-pending.json").exists())
            self.assertFalse((country_dir / "app-catalog-final.json").exists())
            self.assertFalse((country_dir / "differences-pending.json").exists())

    def test_incomplete_batch_is_rejected_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pending_path = self.create_pending(root)
            entries = discover_pending_catalogues(root)
            final_batch = build_rarity_batch(entries)
            final_batch["countries"][0]["coins"].pop()
            final_batch["countries"][0]["coins"][0]["availability"] = "historical"

            with self.assertRaisesRegex(ValueError, "Falta classificar"):
                apply_rarity_batch(final_batch, entries)

            self.assertFalse((pending_path.parent / "app-catalog.json").exists())

    def test_country_with_final_catalogue_is_not_included(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pending_path = self.create_pending(root)
            (pending_path.parent / "app-catalog.json").write_text("{}", encoding="utf-8")

            self.assertEqual(discover_pending_catalogues(root), [])


if __name__ == "__main__":
    unittest.main()
