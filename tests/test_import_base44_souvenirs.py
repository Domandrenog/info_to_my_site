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


def pennycollector_souvenir(position: int, **overrides):
    record = souvenir()
    record.update(
        {
            "name": f"Design {position}",
            "location_name": "Kennedy Space Center",
            "notes": f"Machine 6 · Posição {position} · PennyCollector location 1851",
            "reference_url": (
                "http://locations.pennycollector.com/Details.aspx?location=1851"
                f"#machine-6-position-{position}"
            ),
        }
    )
    record.update(overrides)
    return record


class ImportBase44SouvenirsTests(unittest.TestCase):
    def test_general_import_uses_presscoins_and_approved_pennycollector_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            presscoins_pending = root / "disney" / "presscoins-catalog.json"
            presscoins = root / "disney" / "presscoins-catalog-final.json"
            pending = root / "nasa" / "pennycollector-catalog.json"
            final = root / "nasa" / "pennycollector-catalog-final.json"
            incomplete = root / "other" / "pennycollector-catalog-final.json"
            for path in (presscoins_pending, presscoins, pending, final, incomplete):
                path.parent.mkdir(parents=True, exist_ok=True)
            presscoins_pending.write_text(
                json.dumps({"source": {"site": "Presscoins"}, "items": []}),
                encoding="utf-8",
            )
            presscoins.write_text(
                json.dumps(
                    {
                        "source": {"site": "Presscoins"},
                        "import_ready": True,
                        "items": [],
                    }
                ),
                encoding="utf-8",
            )
            pending.write_text(
                json.dumps({"source": {"site": "PennyCollector"}, "items": []}),
                encoding="utf-8",
            )
            final.write_text(
                json.dumps(
                    {
                        "source": {"site": "PennyCollector"},
                        "import_ready": True,
                        "items": [],
                    }
                ),
                encoding="utf-8",
            )
            incomplete.write_text(
                json.dumps(
                    {
                        "source": {"site": "PennyCollector"},
                        "import_ready": False,
                        "items": [],
                    }
                ),
                encoding="utf-8",
            )

            discovered = import_base44_souvenirs.discover_import_catalogs(root)

        self.assertEqual(discovered, sorted([presscoins, final]))

    def test_combined_catalogues_deduplicate_the_same_souvenir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = [root / "first.json", root / "second.json"]
            record = souvenir()
            for index, path in enumerate(paths, start=1):
                duplicate = dict(record, ordem=index)
                path.write_text(
                    json.dumps({"items": [{"souvenir": duplicate}]}),
                    encoding="utf-8",
                )

            records, duplicate_count = import_base44_souvenirs.combined_catalogue_records(
                paths
            )

        self.assertEqual(records, [record])
        self.assertEqual(duplicate_count, 1)

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

    def test_pennycollector_designs_on_same_page_have_distinct_identity(self) -> None:
        first = pennycollector_souvenir(1)
        second = pennycollector_souvenir(2)

        records = import_base44_souvenirs.catalogue_records(
            {
                "items": [
                    {"review_status": "approved", "souvenir": first},
                    {"review_status": "approved", "souvenir": second},
                ]
            }
        )

        self.assertEqual(records, [first, second])
        self.assertNotEqual(
            import_base44_souvenirs.preferred_identity(first),
            import_base44_souvenirs.preferred_identity(second),
        )

    def test_legacy_pennycollector_record_without_position_matches_by_fields(self) -> None:
        pending = pennycollector_souvenir(1)
        existing = pennycollector_souvenir(
            1,
            notes="",
            reference_url=(
                "http://locations.pennycollector.com/Details.aspx?location=1851"
            ),
        )

        missing, already_present = import_base44_souvenirs.partition_missing(
            [pending], [existing]
        )

        self.assertEqual(missing, [])
        self.assertEqual(already_present, [pending])

    def test_skipped_review_items_are_not_imported(self) -> None:
        approved = pennycollector_souvenir(1)
        skipped = pennycollector_souvenir(2)

        records = import_base44_souvenirs.catalogue_records(
            {
                "items": [
                    {"review_status": "approved", "souvenir": approved},
                    {"review_status": "skipped", "souvenir": skipped},
                ]
            }
        )

        self.assertEqual(records, [approved])

    def test_pending_review_item_is_rejected_by_importer(self) -> None:
        with self.assertRaisesRegex(ValueError, "ainda pendente"):
            import_base44_souvenirs.catalogue_records(
                {
                    "items": [
                        {
                            "review_status": "pending",
                            "souvenir": pennycollector_souvenir(1),
                        }
                    ]
                }
            )

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

    def test_loading_existing_souvenirs_reports_progress_by_country(self) -> None:
        class FakeClient:
            def filter(self, query, *, limit, skip):
                self.assert_valid_pagination(limit, skip)
                return [{"country": query["country"]}]

            @staticmethod
            def assert_valid_pagination(limit, skip):
                if (limit, skip) != (1000, 0):
                    raise AssertionError((limit, skip))

        records = [
            souvenir(country="EUA"),
            souvenir(country="Portugal", ordem=2),
        ]
        with redirect_stdout(io.StringIO()) as output:
            existing = import_base44_souvenirs.load_existing(
                FakeClient(), records, show_progress=True
            )

        progress = output.getvalue()
        self.assertEqual(len(existing), 2)
        self.assertIn("1/2 — EUA: a consultar", progress)
        self.assertIn("2/2 — Portugal: a consultar", progress)
        self.assertEqual(progress.count("1 encontrados"), 2)

    def test_plan_shows_exact_import_impact_by_country(self) -> None:
        existing = souvenir(country="EUA", name="Already there")
        portugal = [
            souvenir(country="Portugal", name="Novo 1", ordem=2),
            souvenir(country="Portugal", name="Novo 2", ordem=3),
        ]

        with redirect_stdout(io.StringIO()) as output:
            import_base44_souvenirs.print_plan(
                [existing, *portugal],
                portugal,
                [existing],
            )

        plan = output.getvalue()
        self.assertIn(
            "EUA: 1 no catálogo · 1 já existente · 0 novos — sem alterações",
            plan,
        )
        self.assertIn(
            "Portugal: 2 no catálogo · 0 já existentes · 2 novos a criar",
            plan,
        )
        self.assertIn("serão criados registos apenas em:\n- Portugal: 2 souvenirs", plan)
        self.assertIn("Países sem alterações: EUA.", plan)
        self.assertIn("Primeiros registos a criar em Portugal:", plan)

    @patch("builtins.input", return_value="")
    def test_write_confirmation_defaults_to_no(self, _input) -> None:
        self.assertFalse(import_base44_souvenirs.ask_confirmation(10))


if __name__ == "__main__":
    unittest.main()
