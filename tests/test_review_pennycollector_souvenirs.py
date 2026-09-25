from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from scripts.review_pennycollector_souvenirs import (
    APPROVED,
    PENDING,
    SHARED_PHOTO_NOTE,
    SKIPPED,
    approve_all,
    discover_pending_catalogs,
    interactive_review,
    merge_previous_review,
    review_summary,
    write_review,
)


def pending_catalog() -> dict:
    items = []
    for position, name in ((1, "Astronaut"), (2, "Rocket")):
        items.append(
            {
                "source": {
                    "location_id": "1851",
                    "machine_number": 6,
                    "position": position,
                    "image_scope": "machine",
                },
                "souvenir": {
                    "name": name,
                    "continent": "América",
                    "country": "EUA",
                    "city": "Merritt Island",
                    "type": "pressed",
                    "condition": "Não Tenho",
                    "location_name": "Kennedy Space Center",
                    "description": name,
                    "display_shape": "oval",
                    "image_front": "http://locations.pennycollector.com/images/machine.jpg",
                    "image_back": "",
                    "notes": f"Machine 6 · Posição {position}",
                    "reference_url": "http://locations.pennycollector.com/Details.aspx?location=1851",
                    "ordem": position,
                    "hidden": False,
                },
                "review_status": "pending",
            }
        )
    return {
        "status": "pending_review",
        "base44_updated": False,
        "import_ready": False,
        "source": {"site": "PennyCollector", "location_name": "Kennedy Space Center"},
        "items": items,
    }


class ReviewPennyCollectorSouvenirsTests(unittest.TestCase):
    def test_general_review_discovers_only_pending_source_catalogs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "portugal" / "location" / "pennycollector-catalog.json"
            final = first.with_name("pennycollector-catalog-final.json")
            first.parent.mkdir(parents=True)
            first.write_text("{}", encoding="utf-8")
            final.write_text("{}", encoding="utf-8")

            self.assertEqual(discover_pending_catalogs(root), [first])

    def test_approve_all_keeps_machine_photo_and_creates_unique_references(self) -> None:
        catalog = pending_catalog()

        approve_all(catalog)
        summary = review_summary(catalog)

        self.assertEqual(summary, {APPROVED: 2, SKIPPED: 0, PENDING: 0})
        references = {item["souvenir"]["reference_url"] for item in catalog["items"]}
        self.assertEqual(len(references), 2)
        self.assertTrue(all("#machine-6-position-" in value for value in references))
        self.assertTrue(
            all(SHARED_PHOTO_NOTE in item["souvenir"]["notes"] for item in catalog["items"])
        )

    def test_individual_review_can_rename_and_skip(self) -> None:
        catalog = pending_catalog()
        answers = iter(["1", "2", "Astronauta na Lua", "3"])
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "pennycollector-catalog-final.json"

            summary = interactive_review(
                catalog,
                output,
                input_fn=lambda _prompt: next(answers),
            )

            self.assertTrue(output.is_file())
            self.assertTrue(output.with_name("review.html").is_file())
        self.assertEqual(summary, {APPROVED: 1, SKIPPED: 1, PENDING: 0})
        self.assertEqual(catalog["items"][0]["souvenir"]["name"], "Astronauta na Lua")
        self.assertEqual(catalog["items"][1]["review_status"], SKIPPED)

    def test_partial_review_is_resumed_by_stable_machine_position(self) -> None:
        pending = pending_catalog()
        previous = copy.deepcopy(pending)
        previous["items"][0]["review_status"] = APPROVED
        previous["items"][0]["souvenir"]["name"] = "Nome corrigido"

        merged = merge_previous_review(pending, previous)

        self.assertEqual(merged["items"][0]["review_status"], APPROVED)
        self.assertEqual(merged["items"][0]["souvenir"]["name"], "Nome corrigido")
        self.assertEqual(merged["items"][1]["review_status"], PENDING)

    def test_review_preview_labels_retired_machine_explicitly(self) -> None:
        catalog = pending_catalog()
        for item in catalog["items"]:
            item["source"]["availability"] = "retired"
        approve_all(catalog)
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "pennycollector-catalog-final.json"
            _catalog_path, preview_path = write_review(catalog, output)
            preview = preview_path.read_text(encoding="utf-8")

        self.assertIn("Máquina retirada 6", preview)
        self.assertNotIn(">Machine 6 ·", preview)

    def test_complete_review_file_is_marked_import_ready(self) -> None:
        catalog = pending_catalog()
        approve_all(catalog)
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "pennycollector-catalog-final.json"
            write_review(catalog, output)

        self.assertTrue(catalog["import_ready"])
        self.assertEqual(catalog["status"], "ready_for_import")
        self.assertFalse(catalog["base44_updated"])


if __name__ == "__main__":
    unittest.main()
