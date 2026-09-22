from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.presscoins_souvenirs import (
    build_catalog,
    build_search_url,
    default_output_directory,
    parse_search_results,
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


class PresscoinsSouvenirsTests(unittest.TestCase):
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
            country="Estados Unidos da América",
            city="Orlando",
        )

        self.assertFalse(catalog["base44_updated"])
        self.assertEqual(catalog["status"], "pending_review")
        souvenir = catalog["items"][0]["souvenir"]
        self.assertEqual(souvenir["name"], "Minnie & Mickey — 2026")
        self.assertEqual(souvenir["type"], "pressed")
        self.assertEqual(souvenir["condition"], "Não Tenho")
        self.assertEqual(souvenir["display_shape"], "oval")
        self.assertEqual(souvenir["image_front"], coin.image_url)
        self.assertEqual(souvenir["image_back"], "")
        self.assertIn("WDW26011", souvenir["reference_url"])

    def test_default_output_is_grouped_by_location_and_search(self) -> None:
        self.assertEqual(
            default_output_directory("Magic Kingdom", "2026"),
            Path("info/souvenirs/america/eua/orlando/magic-kingdom/2026"),
        )

    def test_outputs_include_json_and_visual_preview(self) -> None:
        coin = parse_search_results(SAMPLE_HTML)[0]
        catalog = build_catalog(
            [coin],
            query_url="https://example.test/search",
            location="Magic Kingdom",
            search="2026",
            country="Estados Unidos da América",
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
