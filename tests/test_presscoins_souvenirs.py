from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.presscoins_souvenirs import (
    build_catalog,
    build_search_url,
    collect_search_results,
    default_output_directory,
    pagination_page_count,
    parse_args,
    parse_search_results,
    subject_from_description,
    write_outputs,
)


SAMPLE_HTML = """
<table class="row_alt">
  <tr>
    <td>
      <a class="lightbox" href="../images/coin_images/Magic Kingdom/large/WDW26011.jpg">
        <img class="thumbnail" src="../images/coin_images/Magic Kingdom/small/WDW26011.gif">
      </a>
    </td>
    <td>
      <b>Location:</b>&nbsp;Magic Kingdom, Emporium #1
      <b>Description:</b>&nbsp;Minnie &amp; Mickey standing beside a 2026 sign
      <b>Position:</b>&nbsp;1<br>
      <b>Coin Type:</b>&nbsp;Cent<br>
      <b>Orientation:</b>&nbsp;H<br>
      <b>Availability:</b>&nbsp;Current<br>
      <b>Catalog Number:</b> WDW26011
    </td>
  </tr>
</table>
<table class="row">
  <tr><td>
    <b>Location:</b>&nbsp;Magic Kingdom, Test
    <b>Description:</b>&nbsp;Goofy looking right
    <b>Position:</b>&nbsp;2<br>
    <b>Coin Type:</b>&nbsp;Cent<br>
    <b>Orientation:</b>&nbsp;V<br>
    <b>Availability:</b>&nbsp;Retired<br>
    <b>Catalog Number:</b> WDW00002
  </td></tr>
</table>
"""


def page_html(catalog_number: str, page_options: str = "") -> str:
    return f"""
    <select id="jumpMenu3">{page_options}</select>
    <table class="row">
      <tr><td>
        <b>Location:</b>&nbsp;Magic Kingdom, Test
        <b>Description:</b>&nbsp;Mickey looking right
        <b>Position:</b>&nbsp;1<br>
        <b>Coin Type:</b>&nbsp;Cent<br>
        <b>Orientation:</b>&nbsp;V<br>
        <b>Availability:</b>&nbsp;Current<br>
        <b>Catalog Number:</b> {catalog_number}
      </td></tr>
    </table>
    """


class PresscoinsSouvenirsTests(unittest.TestCase):
    def test_default_souvenir_country_matches_base44(self) -> None:
        self.assertEqual(parse_args([]).country, "EUA")

    def test_long_descriptions_produce_short_display_names(self) -> None:
        examples = {
            "Pongo & Perdita, straight lined chest and has very fine lines, (NOTE: old die)": "Pongo & Perdita",
            "Woody and Jessie riding Bullseye \"WOODY\" at top": "Woody and Jessie",
            "Hitchhiking Ghost Gus (Prisoner), Haunted Mansion logo": "Hitchhiking Ghost Gus",
            "Pirate Pluto wearing a bandana and earring, Pirates logo": "Pirate Pluto",
            "Lady \"Walt Disney's Lady and the Tramp / 1 of 6\"": "Lady",
            "Cowboy Stitch swing a lasso overhead \"PECOS BILL\"": "Cowboy Stitch",
        }
        for description, expected in examples.items():
            with self.subTest(description=description):
                self.assertEqual(subject_from_description(description), expected)

    def test_display_name_has_a_hard_readable_length_limit(self) -> None:
        description = (
            "A deliberately extremely long souvenir design subject containing many words "
            "before any description details"
        )

        self.assertLessEqual(len(subject_from_description(description)), 48)

    def test_parser_extracts_records_and_large_image_url(self) -> None:
        coins = parse_search_results(SAMPLE_HTML)

        self.assertEqual(len(coins), 2)
        self.assertEqual(coins[0].catalog_number, "WDW26011")
        self.assertEqual(coins[0].location, "Magic Kingdom, Emporium #1")
        self.assertEqual(coins[0].description, "Minnie & Mickey standing beside a 2026 sign")
        self.assertEqual(
            coins[0].image_url,
            "https://www.presscoins.com/images/coin_images/Magic%20Kingdom/large/WDW26011.jpg",
        )
        self.assertEqual(coins[1].image_url, "")

    def test_catalog_uses_direct_photo_link_without_updating_base44(self) -> None:
        coin = parse_search_results(SAMPLE_HTML)[0]
        query_url = build_search_url(location="Magic Kingdom", search="2026")

        catalog = build_catalog(
            [coin],
            query_url=query_url,
            location="Magic Kingdom",
            search="2026",
            country="EUA",
            city="Orlando",
        )

        self.assertFalse(catalog["base44_updated"])
        self.assertEqual(catalog["status"], "pending_review")
        souvenir = catalog["items"][0]["souvenir"]
        self.assertEqual(souvenir["name"], "Minnie & Mickey — 2026")
        self.assertEqual(souvenir["country"], "EUA")
        self.assertEqual(souvenir["type"], "pressed")
        self.assertEqual(souvenir["condition"], "Não Tenho")
        self.assertEqual(souvenir["display_shape"], "oval")
        self.assertEqual(souvenir["location_name"], "Magic Kingdom")
        self.assertEqual(
            souvenir["notes"],
            "Emporium #1 · Posição 1 · Catálogo Presscoins: WDW26011",
        )
        self.assertEqual(souvenir["image_front"], coin.image_url)
        self.assertEqual(souvenir["image_back"], "")
        self.assertIn("WDW26011", souvenir["reference_url"])

    def test_default_output_is_grouped_by_location_and_search(self) -> None:
        self.assertEqual(
            default_output_directory("Magic Kingdom", "2026", "All"),
            Path("info/souvenirs/america/eua/orlando/magic-kingdom/2026"),
        )

    def test_empty_search_uses_all_folder(self) -> None:
        self.assertEqual(
            default_output_directory("Magic Kingdom", "", "All"),
            Path("info/souvenirs/america/eua/orlando/magic-kingdom/todas"),
        )

    def test_availability_scopes_have_separate_output_folders(self) -> None:
        self.assertEqual(
            default_output_directory("Magic Kingdom", "", "1"),
            Path("info/souvenirs/america/eua/orlando/magic-kingdom/atuais"),
        )
        self.assertEqual(
            default_output_directory("Magic Kingdom", "", "0"),
            Path("info/souvenirs/america/eua/orlando/magic-kingdom/retiradas"),
        )
        self.assertEqual(
            default_output_directory("Magic Kingdom", "2026", "1"),
            Path("info/souvenirs/america/eua/orlando/magic-kingdom/2026/atuais"),
        )

    def test_pagination_collects_all_pages_and_removes_duplicates(self) -> None:
        options = "".join(
            f'<option value="?search=&amp;page={page}">{page}</option>'
            for page in (1, 2, 3)
        )
        pages = {
            1: page_html("WDW00001", options),
            2: page_html("WDW00002", options),
            3: page_html("WDW00002", options),
        }

        def fetcher(url: str) -> str:
            page = int(url.rsplit("page=", 1)[1])
            return pages[page]

        coins, total_pages = collect_search_results(
            location="Magic Kingdom",
            search="",
            availability="All",
            coin_type="All",
            max_pages=10,
            fetcher=fetcher,
        )

        self.assertEqual(pagination_page_count(pages[1]), 3)
        self.assertEqual(total_pages, 3)
        self.assertEqual([coin.catalog_number for coin in coins], ["WDW00001", "WDW00002"])

    def test_pagination_respects_safety_limit(self) -> None:
        options = '<option value="?search=&amp;page=4">4</option>'
        with self.assertRaisesRegex(ValueError, "limite de segurança"):
            collect_search_results(
                location="Magic Kingdom",
                search="",
                availability="All",
                coin_type="All",
                max_pages=3,
                fetcher=lambda _url: page_html("WDW00001", options),
            )

    def test_outputs_include_json_and_visual_preview(self) -> None:
        coin = parse_search_results(SAMPLE_HTML)[0]
        catalog = build_catalog(
            [coin],
            query_url="https://example.test/search",
            location="Magic Kingdom",
            search="2026",
            country="EUA",
            city="Orlando",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            catalog_path, preview_path = write_outputs(catalog, Path(temp_dir))

            self.assertTrue(catalog_path.is_file())
            preview = preview_path.read_text(encoding="utf-8")
            self.assertIn(coin.image_url, preview)
            self.assertIn("Nenhuma alteração foi feita no Site Base44", preview)


if __name__ == "__main__":
    unittest.main()
