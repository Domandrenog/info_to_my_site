from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.pennycollector_souvenirs import (
    build_catalog,
    default_output_directory,
    parse_designs,
    preview_html,
    write_outputs,
)


SAMPLE_HTML = """
<input id="ReportLocation_Location" value="Kennedy Space Center Visitor Complex">
<input id="ReportLocation_Address" value="Space Commerce Way">
<input id="ReportLocation_City" value="Merritt Island">
<input id="ReportLocation_Zip" value="32953">
<select id="ReportLocation_StateList"><option selected="selected">FL</option></select>
<select id="ReportLocation_StatusList"><option selected="selected">Active</option></select>
Needs Updating
<b>Active machines</b>
<b>Machine 6:</b> Temp Space Shop.<br>
The designs are: 1) Astronaut on the moon with U.S. flag (V);
2) NASA logo on U.S. flag background (H);
3) Telescope with “reach for the stars” (V);
4) Space Shuttle landing (H).<p>
<b>Machine 18:</b> 2023:<br>
1) 'Artemus', Logo, 2) 'Spaceport KSC', Logo, 3) 'Gateway', logo,
4) 'Orion', an image of spacecraft.<p>
<b>Medallion/ Token machines:</b>
<span class="pagetitle">Machine 6: Temp Space Shop</span><br>
<img src="images/machine-6.jpg">
<span class="pagetitle">Machine 18 (2023)</span><br>
<img src="images/machine-18.jpg">
"""


class PennyCollectorSouvenirsTests(unittest.TestCase):
    def test_active_designs_and_machine_photos_are_extracted(self) -> None:
        designs, metadata = parse_designs(SAMPLE_HTML)

        self.assertEqual(len(designs), 8)
        self.assertEqual({design.machine_number for design in designs}, {6, 18})
        self.assertEqual(designs[0].description, "Astronaut on the moon with U.S. flag")
        self.assertEqual(designs[0].orientation, "V")
        self.assertEqual(
            designs[0].machine_image_url,
            "http://locations.pennycollector.com/images/machine-6.jpg",
        )
        self.assertEqual(designs[-1].orientation, "")
        self.assertEqual(metadata["location_name"], "Kennedy Space Center Visitor Complex")
        self.assertEqual(metadata["city"], "Merritt Island")
        self.assertEqual(metadata["status"], "Active")
        self.assertTrue(metadata["source_flagged_needs_update"])

    def test_catalog_is_review_only_and_uses_machine_photo_scope(self) -> None:
        designs, metadata = parse_designs(SAMPLE_HTML)

        catalog = build_catalog(designs, metadata, location_id="1851")

        self.assertFalse(catalog["base44_updated"])
        self.assertFalse(catalog["import_ready"])
        self.assertEqual(catalog["source"]["machine_count"], 2)
        self.assertEqual(catalog["source"]["design_count"], 8)
        first = catalog["items"][0]
        self.assertEqual(first["souvenir"]["country"], "EUA")
        self.assertEqual(first["souvenir"]["city"], "Merritt Island")
        self.assertEqual(first["souvenir"]["location_name"], "Kennedy Space Center")
        self.assertEqual(first["source"]["image_scope"], "machine")
        self.assertIn("shared_machine_photo", first["source"]["review_flags"])
        self.assertIn(
            "missing_orientation", catalog["items"][-1]["source"]["review_flags"]
        )
        self.assertIn(
            "possible_source_typo", catalog["items"][-4]["source"]["review_flags"]
        )
        self.assertEqual(catalog["items"][-4]["souvenir"]["name"], "Artemus")

    def test_preview_groups_designs_by_machine_and_warns_about_photos(self) -> None:
        designs, metadata = parse_designs(SAMPLE_HTML)
        catalog = build_catalog(designs, metadata, location_id="1851")

        preview = preview_html(catalog)

        self.assertEqual(preview.count('<section class="machine">'), 2)
        self.assertIn("A fotografia abaixo pertence à máquina", preview)
        self.assertIn("nenhuma alteração efetuada", preview)
        self.assertIn("Needs Updating", preview)
        self.assertIn("Machine 6", preview)
        self.assertIn("Machine 18", preview)

    def test_outputs_are_kept_outside_magic_kingdom(self) -> None:
        self.assertEqual(
            default_output_directory(),
            Path(
                "info/souvenirs/america/eua/merritt-island/"
                "kennedy-space-center"
            ),
        )
        designs, metadata = parse_designs(SAMPLE_HTML)
        catalog = build_catalog(designs, metadata, location_id="1851")
        with tempfile.TemporaryDirectory() as temp_dir:
            catalog_path, preview_path = write_outputs(catalog, Path(temp_dir))

            self.assertTrue(catalog_path.is_file())
            self.assertTrue(preview_path.is_file())
            self.assertEqual(catalog_path.name, "pennycollector-catalog.json")


if __name__ == "__main__":
    unittest.main()
