from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.pennycollector_souvenirs import (
    DesignSequence,
    build_catalog,
    choose_machine_sequences,
    country_settings,
    default_output_directory,
    parse_designs,
    preview_html,
    reference_id,
    souvenir_type_for_machine_details,
    write_outputs,
)


SAMPLE_HTML = """
<input id="ReportLocation_Location" value="Kennedy Space Center Visitor Complex">
<input id="ReportLocation_Address" value="Space Commerce Way">
<input id="ReportLocation_City" value="Merritt Island">
<input id="ReportLocation_Zip" value="32953">
<select id="ReportLocation_StateList"><option selected="selected">FL</option></select>
<select id="ReportLocation_CountryList"><option selected="selected">United States</option></select>
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

SIMPLE_HTML = """
<input id="ReportLocation_Location" value="Manatee Viewing Center Gift Shop">
<input id="ReportLocation_Address" value="6990 Dickman Road">
<input id="ReportLocation_City" value="Apollo Beach">
<input id="ReportLocation_Machine1_MachineName" value="Machine 1">
<select id="ReportLocation_CountryList"><option selected="selected">United States</option></select>
<select id="ReportLocation_StateList"><option selected="selected">FL</option></select>
<select id="ReportLocation_StatusList"><option selected="selected">Active</option></select>
<table><tr><td id="DescriptionContainer">Description.<p>Designs are: all designs say Manatee Viewing Center.<br>
1) Manatee<br>2) 2 Pelicans<br>3) 2 Manatees<br>4) a Snook (Fish)<p>Gift shop text.</td></tr></table>
<span class="pagetitle">Machine 1</span><br><img src="images/manatee.jpg">
"""


NARRATIVE_HTML = """
<input id="ReportLocation_Location" value="Hard Rock Cafe">
<input id="ReportLocation_Address" value="401 Biscayne Boulevard Suite R-200">
<input id="ReportLocation_City" value="Miami">
<select id="ReportLocation_CountryList"><option selected="selected">United States</option></select>
<select id="ReportLocation_StateList"><option selected="selected">FL</option></select>
<select id="ReportLocation_StatusList"><option selected="selected">Gone</option></select>
<table><tr><td id="DescriptionContainer"><p>Location introduction.</p>
<b>Machine 1</b> is inside the Rock Shop. Designs all have a beaded border:<br>
1. (V) Miami city skyline inside a guitar pick.<br>
2. (H) Hard Rock Cafe logo.<br>
3. (H) Tee shirt with logo.<br>
4. (H) Gibson Les Paul guitar.<p>
G.P.S. coordinates and later location updates.</td></tr></table>
<span class="pagetitle">Machine 1 - Inside Rock Shop</span><br>
<img src="images/machine-miami.jpg">
"""


MULTI_MACHINE_NARRATIVE_HTML = """
<input id="ReportLocation_Location" value="Faneuil Hall Marketplace, Quincy Market">
<input id="ReportLocation_City" value="Boston">
<select id="ReportLocation_CountryList"><option selected="selected">United States</option></select>
<select id="ReportLocation_StatusList"><option selected="selected">Active</option></select>
<table><tr><td id="DescriptionContainer">
<b>Machine 3</b> is outdoors. Designs are:<br>
1) Design 3-1<br>2) Design 3-2<br>3) Design 3-3<br>4) Design 3-4<p>
<b>Machine 4</b> is inside. Designs are:<br>
1) Design 4-1<br>2) Design 4-2<br>3) Design 4-3<br>4) Design 4-4<p>
<b>Machine 6:</b> is near the restrooms. Designs are:<br>
1) Design 6-1<br>2) Design 6-2<br>3) Design 6-3<br>4) Design 6-4<p>
<b>Retired machines:</b><p>Retired 1: 1) Old 1, 2) Old 2, 3) Old 3, 4) Old 4</td></tr></table>
<span class="pagetitle">Machine 3 - outdoors</span><img src="images/machine-3.jpg">
<span class="pagetitle">Machine 4 - inside</span><img src="images/machine-4.jpg">
<span class="pagetitle">Machine 6 - north side</span><img src="images/machine-6.jpg">
<span class="pagetitle">Retired 1</span><img src="images/retired-1.jpg">
"""


MIXED_FORMAT_HTML = """
<input id="ReportLocation_Location" value="Mixed Souvenir Shop">
<input id="ReportLocation_City" value="Albufeira">
<input id="ReportLocation_Machine1_MachineName" value="Machine 1">
<input id="ReportLocation_Machine2_MachineName" value="Token Machine 1">
<select id="ReportLocation_Machine1_QuantityDrop"><option selected="selected">3</option></select>
<select id="ReportLocation_Machine2_QuantityDrop"><option selected="selected">1</option></select>
<select id="ReportLocation_CountryList"><option selected="selected">Portugal</option></select>
<table><tr><td id="DescriptionContainer">Designs are: 1, Beach, 2, Tunnel, 3, Museum,
Token Machine 1: Design: 1) Skyline.</td></tr></table>
<span class="pagetitle">Machine 1</span><img src="images/euro.jpg">
<span class="pagetitle">Token Machine 1</span><img src="images/token.jpg">
"""


MULTI_TOKEN_HTML = """
<input id="ReportLocation_Location" value="Three Token Machines">
<input id="ReportLocation_Machine1_MachineName" value="Token Machine 1">
<input id="ReportLocation_Machine2_MachineName" value="Token Machine 2">
<input id="ReportLocation_Machine3_MachineName" value="Token Machine 3">
<select id="ReportLocation_Machine1_QuantityDrop"><option selected="selected">2</option></select>
<select id="ReportLocation_Machine2_QuantityDrop"><option selected="selected">2</option></select>
<select id="ReportLocation_Machine3_QuantityDrop"><option selected="selected">2</option></select>
<table><tr><td id="DescriptionContainer">
Token Machine 1: Designs are: 1) First A, 2) First B.
Token Machine 2: Designs are: 1) Second A, 2) Second B.
Token Machine 3: Designs are: 1) Third A, 2) Third B. 6/12: still present.
</td></tr></table>
<span class="pagetitle">Token Machine 1</span><img src="images/token-1.jpg">
<span class="pagetitle">Token Machine 2</span><img src="images/token-2.jpg">
<span class="pagetitle">Token Machine 3</span><img src="images/token-3.jpg">
"""


TOKEN_SIDES_HTML = """
<input id="ReportLocation_Location" value="Token Sides">
<input id="ReportLocation_Machine1_MachineName" value="Token Machine 1">
<select id="ReportLocation_Machine1_QuantityDrop"><option selected="selected">2</option></select>
<table><tr><td id="DescriptionContainer">Design:
Token 1 Obverse: Cable car. Token 1 Reverse: Mountain.
Token 2 Obverse: Train. Token 2 Reverse: Museum.
10/16/25: machine checked.
</td></tr></table>
<span class="pagetitle">Token Machine 1</span><img src="images/token-sides.jpg">
"""


RETIRED_VARIANTS_HTML = """
<input id="ReportLocation_Location" value="Retired Variants">
<input id="ReportLocation_Machine1_MachineName" value="Machine 3">
<input id="ReportLocation_Machine2_MachineName" value="Machine 1 - Retired">
<input id="ReportLocation_Machine3_MachineName" value="Retired Token Machine 2">
<select id="ReportLocation_Machine1_QuantityDrop"><option selected="selected">1</option></select>
<select id="ReportLocation_Machine2_QuantityDrop"><option selected="selected">?</option></select>
<select id="ReportLocation_Machine3_QuantityDrop"><option selected="selected">?</option></select>
<table><tr><td id="DescriptionContainer">Machine 3: Designs are: 1) Current train.
Retired machines/ designs:
Machine 1 design had a border: Old locomotive.
Retired Token Machine 2: 1. Old token A. 2. Old token B.
</td></tr></table>
<span class="pagetitle">Machine 3</span><img src="images/current.jpg">
<span class="pagetitle">Machine 1 - Retired</span><img src="images/retired-1.jpg">
<span class="pagetitle">Retired Token Machine 2</span><img src="images/retired-2.jpg">
"""


MISSING_DESCRIPTIONS_HTML = """
<input id="ReportLocation_Location" value="No Written Designs">
<input id="ReportLocation_Machine1_MachineName" value="Machine 1">
<select id="ReportLocation_Machine1_QuantityDrop"><option selected="selected">2</option></select>
<table><tr><td id="DescriptionContainer">The machine is beside the entrance.</td></tr></table>
<span class="pagetitle">Machine 1</span><img src="images/no-description.jpg">
"""


class PennyCollectorSouvenirsTests(unittest.TestCase):
    def test_known_global_country_uses_project_label_and_path(self) -> None:
        self.assertEqual(
            country_settings("Brazil"), ("América", "america", "Brasil", "brasil")
        )
        self.assertEqual(
            country_settings("Japan"), ("Ásia", "asia", "Japão", "japao")
        )

    def test_location_reference_accepts_id_or_full_url(self) -> None:
        self.assertEqual(reference_id("1851", "location"), "1851")
        self.assertEqual(
            reference_id(
                "http://locations.pennycollector.com/Details.aspx?location=1851",
                "location",
            ),
            "1851",
        )

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

    def test_simple_location_without_active_heading_is_supported(self) -> None:
        designs, metadata = parse_designs(SIMPLE_HTML)

        self.assertEqual(len(designs), 4)
        self.assertEqual(metadata["location_name"], "Manatee Viewing Center Gift Shop")
        self.assertEqual(designs[0].description, "Manatee")
        self.assertEqual(designs[-1].description, "a Snook (Fish)")
        self.assertEqual(designs[0].orientation, "")
        self.assertTrue(designs[0].machine_image_url.endswith("images/manatee.jpg"))

    def test_gone_location_with_narrative_numbered_designs_is_supported(self) -> None:
        designs, metadata = parse_designs(NARRATIVE_HTML)

        self.assertEqual(len(designs), 4)
        self.assertEqual(metadata["location_name"], "Hard Rock Cafe")
        self.assertEqual(metadata["city"], "Miami")
        self.assertEqual(metadata["status"], "Gone")
        self.assertEqual(designs[0].description, "Miami city skyline inside a guitar pick")
        self.assertEqual(designs[0].orientation, "V")
        self.assertEqual(designs[-1].description, "Gibson Les Paul guitar")
        self.assertEqual(designs[-1].orientation, "H")
        self.assertNotIn("G.P.S.", designs[-1].description)
        self.assertTrue(
            designs[0].machine_image_url.endswith("images/machine-miami.jpg")
        )

    def test_all_current_machines_in_narrative_page_are_extracted(self) -> None:
        designs, metadata = parse_designs(MULTI_MACHINE_NARRATIVE_HTML)

        self.assertEqual(metadata["location_name"], "Faneuil Hall Marketplace, Quincy Market")
        self.assertEqual(len(designs), 12)
        self.assertEqual({design.machine_number for design in designs}, {3, 4, 6})
        self.assertEqual(
            {machine: sum(design.machine_number == machine for design in designs) for machine in (3, 4, 6)},
            {3: 4, 4: 4, 6: 4},
        )
        self.assertTrue(all(design.machine_image_url for design in designs))
        self.assertNotIn("Old 1", {design.description for design in designs})

    def test_retired_machines_are_included_only_when_requested(self) -> None:
        current, _metadata = parse_designs(MULTI_MACHINE_NARRATIVE_HTML)
        all_designs, _metadata = parse_designs(
            MULTI_MACHINE_NARRATIVE_HTML, include_retired=True
        )

        self.assertEqual(len(current), 12)
        self.assertEqual(len(all_designs), 16)
        retired = [
            design for design in all_designs if design.availability == "retired"
        ]
        self.assertEqual(len(retired), 4)
        self.assertEqual({design.machine_number for design in retired}, {1})
        self.assertTrue(
            all(design.machine_image_url.endswith("images/retired-1.jpg") for design in retired)
        )
        catalog = build_catalog(
            all_designs,
            {
                "country": "United States",
                "location_name": "Faneuil Hall Marketplace, Quincy Market",
                "city": "Boston",
            },
            location_id="8143",
        )
        retired_items = [
            item for item in catalog["items"]
            if item["source"]["availability"] == "retired"
        ]
        self.assertTrue(
            all("#retired-machine-1-position-" in item["souvenir"]["reference_url"] for item in retired_items)
        )
        self.assertIn("Máquina retirada 1", preview_html(catalog))

    def test_comma_lists_and_duplicate_public_machine_numbers_are_separated(self) -> None:
        designs, metadata = parse_designs(MIXED_FORMAT_HTML)

        self.assertEqual(len(designs), 4)
        self.assertEqual(
            {(design.machine_number, design.position) for design in designs},
            {(1, 1), (1, 2), (1, 3), (2, 1)},
        )
        self.assertTrue(designs[0].machine_image_url.endswith("images/euro.jpg"))
        self.assertTrue(designs[-1].machine_image_url.endswith("images/token.jpg"))
        catalog = build_catalog(designs, metadata, location_id="406415")
        self.assertNotIn("coin_type", catalog["items"][0]["source"])
        self.assertEqual(catalog["items"][0]["souvenir"]["type"], "pressed")
        self.assertEqual(catalog["items"][-1]["souvenir"]["type"], "coin")

    def test_all_token_machines_are_extracted_and_history_is_removed(self) -> None:
        designs, metadata = parse_designs(MULTI_TOKEN_HTML)

        self.assertEqual(len(designs), 6)
        self.assertEqual(
            {
                machine: sum(design.machine_number == machine for design in designs)
                for machine in (1, 2, 3)
            },
            {1: 2, 2: 2, 3: 2},
        )
        self.assertEqual(designs[-1].description, "Third B")
        self.assertTrue(all(design.machine_image_url for design in designs))
        catalog = build_catalog(designs, metadata, location_id="token-location")
        self.assertTrue(
            all(item["souvenir"]["type"] == "coin" for item in catalog["items"])
        )

    def test_type_detection_requires_an_explicit_token_or_medallion_machine_label(self) -> None:
        self.assertEqual(souvenir_type_for_machine_details("Token Machine 1"), "coin")
        self.assertEqual(souvenir_type_for_machine_details("Medallion Machine 2"), "coin")
        self.assertEqual(
            souvenir_type_for_machine_details("Retired Token Machine 3"), "coin"
        )
        self.assertEqual(
            souvenir_type_for_machine_details(
                "Temp shop with a medallion/token machine beside this machine"
            ),
            "pressed",
        )

    def test_machine_descriptions_can_be_matched_when_form_order_differs(self) -> None:
        group_of_four = DesignSequence(
            start=0,
            end=40,
            entries=tuple((position, f"Main {position}") for position in range(1, 5)),
        )
        group_of_one = DesignSequence(
            start=41, end=50, entries=((1, "Single"),)
        )

        selected = choose_machine_sequences(
            [group_of_four, group_of_one], [1, 4]
        )

        self.assertEqual(selected, [group_of_one, group_of_four])

        another_group_of_one = DesignSequence(
            start=51, end=60, entries=((1, "Another single"),)
        )
        self.assertEqual(
            choose_machine_sequences(
                [group_of_four, group_of_one, another_group_of_one], [1, 4]
            ),
            [],
        )

    def test_token_obverse_and_reverse_pairs_form_two_designs(self) -> None:
        designs, _metadata = parse_designs(TOKEN_SIDES_HTML)

        self.assertEqual(len(designs), 2)
        self.assertEqual(designs[0].description, "Obverse: Cable car / Reverse: Mountain")
        self.assertEqual(designs[1].description, "Obverse: Train / Reverse: Museum")

    def test_retired_header_variants_and_machine_name_suffix_are_supported(self) -> None:
        current, _metadata = parse_designs(RETIRED_VARIANTS_HTML)
        all_designs, _metadata = parse_designs(
            RETIRED_VARIANTS_HTML, include_retired=True
        )

        self.assertEqual(len(current), 1)
        self.assertEqual(len(all_designs), 4)
        retired = [design for design in all_designs if design.availability == "retired"]
        self.assertEqual(
            {(design.machine_number, design.position) for design in retired},
            {(1, 1), (2, 1), (2, 2)},
        )
        self.assertTrue(all(design.machine_image_url for design in retired))
        catalog = build_catalog(
            all_designs,
            {"country": "Portugal", "location_name": "Retired Variants"},
            location_id="retired-variants",
        )
        retired_types = {
            (item["source"]["machine_number"], item["souvenir"]["type"])
            for item in catalog["items"]
            if item["source"]["availability"] == "retired"
        }
        self.assertEqual(retired_types, {(1, "pressed"), (2, "coin")})

    def test_common_reverse_is_applied_to_every_design_in_the_machine(self) -> None:
        source_html = MIXED_FORMAT_HTML.replace(
            "3, Museum,\nToken Machine",
            "3, Museum. Rear- All designs use the same seal.\nToken Machine",
        )
        designs, _metadata = parse_designs(source_html)

        self.assertTrue(
            all(
                "Rear- All designs use the same seal" in design.description
                for design in designs[:3]
            )
        )
        self.assertNotIn("same seal", designs[-1].description)

    def test_declared_designs_without_descriptions_do_not_create_partial_catalog(self) -> None:
        with self.assertRaisesRegex(ValueError, "indica 2 designs atuais.*catálogo parcial"):
            parse_designs(MISSING_DESCRIPTIONS_HTML)

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
        self.assertIn("Fotografia provisória partilhada da máquina", first["souvenir"]["notes"])
        self.assertTrue(
            first["souvenir"]["reference_url"].endswith("#machine-6-position-1")
        )
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
            default_output_directory(
                {
                    "country": "United States",
                    "city": "Merritt Island",
                    "location_name": "Kennedy Space Center Visitor Complex",
                }
            ),
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
