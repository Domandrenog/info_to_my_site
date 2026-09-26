from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import backup


class FakeClient:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def filter(self, query, *, limit, skip):
        self.calls.append((query, limit, skip))
        return self.records[skip : skip + limit]


class Base44BackupTests(unittest.TestCase):
    def test_default_scope_contains_all_documented_entities(self) -> None:
        self.assertEqual(
            backup.selected_entities([], []),
            [
                "CoinVariant",
                "SpecialCoin",
                "CountryNote",
                "CountrySettings",
                "Coin",
                "CoinSighting",
                "Souvenir",
                "User",
            ],
        )
        self.assertEqual(
            backup.selected_entities(["Coin", "Souvenir", "Coin"], ["Souvenir"]),
            ["Coin"],
        )

    def test_pagination_rejects_duplicate_ids(self) -> None:
        client = FakeClient([{"id": "1"}, {"id": "2"}, {"id": "1"}])

        with self.assertRaisesRegex(RuntimeError, "mais de uma página"):
            backup.fetch_entity_records(
                client,
                "Coin",
                page_size=2,
                output_fn=lambda _message: None,
            )

    def test_complete_views_attach_variants_and_who_found_each_item(self) -> None:
        views, summary = backup.build_complete_item_views(
            {
                "Coin": [{"id": "c1", "condition": "Tenho", "url_ucoin": "u"}],
                "Souvenir": [{"id": "s1", "condition": "Não Tenho"}],
                "CoinVariant": [
                    {"id": "v1", "coin_id": "c1", "condition": "Má Qualidade"}
                ],
                "CoinSighting": [
                    {
                        "id": "f1",
                        "coin_id": "c1",
                        "item_id": "c1",
                        "username": "finder",
                    },
                    {"id": "f2", "coin_id": "s1", "username": "finder-2"},
                    {"id": "f3", "coin_id": "missing", "username": "orphan"},
                ],
            }
        )

        self.assertEqual(views["Coin"][0]["record"]["condition"], "Tenho")
        self.assertEqual(views["Coin"][0]["variants"][0]["id"], "v1")
        self.assertEqual(views["Coin"][0]["sightings"][0]["username"], "finder")
        self.assertEqual(views["Souvenir"][0]["sightings"][0]["id"], "f2")
        self.assertEqual(len(views["unmatched-relations"]), 1)
        self.assertEqual(summary["coin_variants"], {"total": 1, "linked": 1, "unmatched": 0})
        self.assertEqual(summary["coin_sightings"], {"total": 3, "linked": 2, "unmatched": 1})

    def test_change_report_preserves_previous_and_current_full_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = Path(temp_dir) / "base44-previous"
            entity_directory = previous / "entities"
            entity_directory.mkdir(parents=True)
            (entity_directory / "Coin.json").write_text(
                json.dumps(
                    [
                        {"id": "c1", "condition": "Não Tenho", "url_ucoin": "old"},
                        {"id": "c3", "condition": "Tenho"},
                    ]
                ),
                encoding="utf-8",
            )

            report = backup.build_change_report(
                previous,
                {
                    "Coin": [
                        {"id": "c1", "condition": "Tenho", "url_ucoin": "new"},
                        {"id": "c2", "condition": "Não Tenho"},
                    ]
                },
                created_at="2026-09-26T18:00:00+00:00",
            )

        changes = report["entities"][0]
        self.assertEqual(report["totals"], {"created": 1, "modified": 1, "deleted": 1, "unchanged": 0})
        self.assertEqual(changes["modified"][0]["changed_fields"], ["condition", "url_ucoin"])
        self.assertEqual(changes["modified"][0]["previous"]["url_ucoin"], "old")
        self.assertEqual(changes["modified"][0]["current"]["url_ucoin"], "new")

    def test_complete_backup_writes_manifest_checksums_and_private_files(self) -> None:
        records = {
            "Coin": [{"id": "2", "name": "Second"}, {"id": "1", "name": "First"}],
            "Souvenir": [{"id": "s1", "name": "Memory"}],
        }
        clients = {entity: FakeClient(values) for entity, values in records.items()}
        created_at = datetime(2026, 9, 26, 10, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "backups"
            destination, manifest = backup.create_backup(
                output_root=root,
                entities=["Coin", "Souvenir"],
                client_factory=lambda entity: clients[entity],
                app_id="app-id",
                server_url="https://example.test",
                page_size=1,
                created_at=created_at,
                output_fn=lambda _message: None,
            )

            self.assertEqual(destination.name, "base44-20260926T103000Z")
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["totals"], {"entities": 2, "records": 3})
            self.assertFalse(manifest["scope"]["binary_assets_downloaded"])
            self.assertTrue(manifest["scope"]["all_api_fields_preserved"])
            self.assertEqual(manifest["relationships"]["items"], 3)
            coin_path = destination / "entities" / "Coin.json"
            coin_records = json.loads(coin_path.read_text(encoding="utf-8"))
            self.assertEqual([record["id"] for record in coin_records], ["1", "2"])
            self.assertEqual((root / "LATEST").read_text().strip(), destination.name)
            self.assertEqual(coin_path.stat().st_mode & 0o777, 0o600)
            checksum_lines = (destination / "SHA256SUMS").read_text().splitlines()
            expected_checksum = hashlib.sha256(coin_path.read_bytes()).hexdigest()
            self.assertIn(f"{expected_checksum}  entities/Coin.json", checksum_lines)
            self.assertTrue((destination / "views" / "Coin-complete.json").exists())
            self.assertTrue((destination / "changes-since-previous.json").exists())
            self.assertEqual(clients["Coin"].calls[-1], ({}, 1, 2))


if __name__ == "__main__":
    unittest.main()
