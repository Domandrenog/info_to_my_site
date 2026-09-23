from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import import_base44_souvenirs
from scripts.import_base44_coins import Base44Client


def souvenir(**overrides):
    record = {
        "name": "Mickey — 2026",
        "continent": "América",
        "country": "EUA",
        "city": "Orlando",
        "type": "pressed",
        "condition": "Não Tenho",
        "location_name": "Magic Kingdom — Emporium #2",
        "description": "Mickey beside 2026",
        "display_shape": "oval",
        "image_front": "https://www.presscoins.com/images/WDW26016.jpg",
        "image_back": "",
        "notes": "Catálogo Presscoins: WDW26016 · Posição: 2",
        "reference_url": "https://www.presscoins.com/search/?search=WDW26016",
        "ordem": 1,
        "hidden": False,
    }
    record.update(overrides)
    return record


class ImportBase44SouvenirsTests(unittest.TestCase):
    def test_catalog_explicitly_not_ready_cannot_be_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pending.json"
            path.write_text(
                json.dumps({"import_ready": False, "items": []}), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "não aprovado para importação"):
                import_base44_souvenirs.read_catalog(path)

    def test_catalog_number_filter_keeps_exact_requested_order(self) -> None:
        first = souvenir(
            name="First",
            notes="Catálogo Presscoins: WDW24087",
            reference_url="https://www.presscoins.com/search/?search=WDW24087",
        )
        second = souvenir(
            name="Second",
            notes="Catálogo Presscoins: WDW18031",
            reference_url="https://www.presscoins.com/search/?search=WDW18031",
        )

        selected = import_base44_souvenirs.filter_records_by_catalog_numbers(
            [first, second], ["WDW18031", "wdw24087", "WDW18031"]
        )

        self.assertEqual(selected, [second, first])

    def test_base44_client_can_target_souvenir_entity(self) -> None:
        client = Base44Client(
            "app-id",
            "api-key",
            request_delay_seconds=0,
            entity_name="Souvenir",
        )

        self.assertTrue(client.base_url.endswith("/entities/Souvenir"))

    def test_catalogue_records_validates_and_keeps_only_schema_fields(self) -> None:
        record = souvenir(temporary_review_value="ignore")

        records = import_base44_souvenirs.catalogue_records(
            {"items": [{"source": {"catalog_number": "WDW26016"}, "souvenir": record}]}
        )

        self.assertEqual(len(records), 1)
        self.assertNotIn("temporary_review_value", records[0])
        self.assertEqual(records[0]["image_front"], record["image_front"])

    def test_invalid_photo_url_is_rejected_before_api_access(self) -> None:
        with self.assertRaisesRegex(ValueError, "image_front"):
            import_base44_souvenirs.catalogue_records(
                {"items": [{"souvenir": souvenir(image_front="local/file.jpg")}]}
            )

    def test_duplicate_presscoins_catalog_number_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "identidades duplicadas"):
            import_base44_souvenirs.catalogue_records(
                {
                    "items": [
                        {"souvenir": souvenir(name="Mickey")},
                        {"souvenir": souvenir(name="Outro nome", ordem=2)},
                    ]
                }
            )

    def test_existing_catalog_number_matches_even_when_reference_url_changed(self) -> None:
        pending = souvenir(reference_url="https://www.presscoins.com/search/?search=WDW26016")
        existing = souvenir(
            reference_url="https://www.presscoins.com/search/?locations=All&search=WDW26016",
            notes="",
        )

        missing, already_present = import_base44_souvenirs.partition_missing(
            [pending], [existing]
        )

        self.assertEqual(missing, [])
        self.assertEqual(already_present, [pending])

    def test_numeric_presscoins_catalog_number_is_read_from_reference_url(self) -> None:
        record = souvenir(
            notes="",
            reference_url="https://www.presscoins.com/search/?search=1996-12",
        )

        self.assertEqual(
            import_base44_souvenirs.presscoins_catalog_number(record),
            "1996-12",
        )

    def test_different_catalog_numbers_do_not_match_only_by_name_and_location(self) -> None:
        pending = souvenir(
            notes="Catálogo Presscoins: WDW26016",
            reference_url="https://www.presscoins.com/search/?search=WDW26016",
        )
        existing = souvenir(
            notes="Catálogo Presscoins: WDW26099",
            reference_url="https://www.presscoins.com/search/?search=WDW26099",
        )

        missing, already_present = import_base44_souvenirs.partition_missing(
            [pending], [existing]
        )

        self.assertEqual(missing, [pending])
        self.assertEqual(already_present, [])

    def test_field_identity_prevents_duplicate_without_reference_url(self) -> None:
        pending = souvenir(reference_url="", notes="")
        existing = souvenir(reference_url="", notes="", image_front="")

        missing, already_present = import_base44_souvenirs.partition_missing(
            [pending], [existing]
        )

        self.assertEqual(missing, [])
        self.assertEqual(already_present, [pending])

    def test_create_missing_records_uses_batches_and_reports_progress(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.batches = []

            def bulk_create(self, records):
                self.batches.append(records)

        client = FakeClient()
        records = [
            souvenir(
                name=f"Souvenir {index}",
                notes=f"Catálogo Presscoins: WDW{index:05d}",
                reference_url=f"https://www.presscoins.com/search/?search=WDW{index:05d}",
                ordem=index,
            )
            for index in range(1, 6)
        ]

        with redirect_stdout(io.StringIO()) as output:
            created = import_base44_souvenirs.create_missing_records(
                client, records, batch_size=2
            )

        self.assertEqual(created, 5)
        self.assertEqual([len(batch) for batch in client.batches], [2, 2, 1])
        self.assertIn("5/5 (100.0%)", output.getvalue())

    @patch("builtins.input", return_value="")
    def test_write_confirmation_defaults_to_no(self, _input) -> None:
        self.assertFalse(import_base44_souvenirs.ask_confirmation(10))


if __name__ == "__main__":
    unittest.main()
