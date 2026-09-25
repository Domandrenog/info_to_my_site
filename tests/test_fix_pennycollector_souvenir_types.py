from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.fix_pennycollector_souvenir_types import (
    apply_corrections,
    build_corrections,
    desired_coin_records,
    prepare_catalog_repairs,
)


def item(name: str, machine_details: str, position: int) -> dict:
    return {
        "source": {
            "machine_details": machine_details,
            "machine_number": position,
            "position": 1,
        },
        "souvenir": {
            "name": name,
            "continent": "Europa",
            "country": "Portugal",
            "city": "Lisboa",
            "type": "pressed",
            "condition": "Não Tenho",
            "location_name": "Local",
            "description": name,
            "reference_url": (
                "http://locations.pennycollector.com/Details.aspx?location=10"
                f"#machine-{position}-position-1"
            ),
        },
        "review_status": "approved",
    }


class FixPennyCollectorSouvenirTypesTests(unittest.TestCase):
    def test_catalog_repairs_and_desired_records_include_only_explicit_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw = root / "pennycollector-catalog.json"
            final = root / "pennycollector-catalog-final.json"
            payload = {
                "source": {"site": "PennyCollector"},
                "import_ready": True,
                "items": [
                    item("Token", "Token Machine 1", 1),
                    item("Pressed", "Shop with a token machine beside it", 2),
                ],
            }
            raw.write_text(json.dumps(payload), encoding="utf-8")
            final.write_text(json.dumps(payload), encoding="utf-8")

            repairs = prepare_catalog_repairs([raw, final])
            desired = desired_coin_records([raw, final])

        self.assertEqual(len(repairs), 2)
        self.assertEqual(sum(repair[2] for repair in repairs), 2)
        self.assertEqual(len(desired), 1)
        self.assertEqual(desired[0]["name"], "Token")
        self.assertEqual(desired[0]["type"], "coin")

    def test_corrections_match_identity_and_update_only_type(self) -> None:
        desired = item("Token", "Token Machine 1", 1)["souvenir"]
        desired["type"] = "coin"
        existing = [{**desired, "id": "record-1", "type": "pressed"}]

        corrections, missing = build_corrections([desired], existing)

        self.assertEqual(missing, [])
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["id"], "record-1")
        self.assertEqual(corrections[0]["current_type"], "pressed")
        self.assertEqual(corrections[0]["desired_type"], "coin")

        class FakeClient:
            def __init__(self) -> None:
                self.updates = []

            def update(self, record_id, payload):
                self.updates.append((record_id, payload))

        client = FakeClient()
        apply_corrections(client, corrections)
        self.assertEqual(client.updates, [("record-1", {"type": "coin"})])


if __name__ == "__main__":
    unittest.main()
