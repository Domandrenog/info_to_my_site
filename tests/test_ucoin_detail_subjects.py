from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts import ucoin_detail_subjects as details


class UcoinDetailSubjectTests(unittest.TestCase):
    def test_extracts_official_subject_from_document_title(self) -> None:
        source = {
            "_catalogCountry": "Portugal",
            "_periodCountry": "Portugal",
        }
        self.assertEqual(
            details.subject_from_document_title(
                "200 escudos 1991 - À Descoberta da América - "
                "Navegações para Ocidente, Portugal - uCoin.net",
                source,
            ),
            "À Descoberta da América - Navegações para Ocidente",
        )
        self.assertEqual(
            details.subject_from_document_title("Notificações", source), ""
        )

    def test_prefers_first_non_interface_heading(self) -> None:
        self.assertEqual(
            details.subject_from_headings(
                [
                    "Notificações",
                    "Adicionar nova moeda",
                    "XXV Jogos Olímpicos de Verão, Barcelona 1992",
                    "Portugal 200 escudos 1992",
                ]
            ),
            "XXV Jogos Olímpicos de Verão, Barcelona 1992",
        )

    def test_accepts_only_a_matching_detail_page_country_and_tid(self) -> None:
        source = {
            "_catalogCountry": "EUA",
            "_periodCountry": "Estados Unidos da América",
        }
        valid = {
            "detailUrl": "https://pt.ucoin.net/coin/usa-1-dollar-2026/?tid=189798",
            "ucoinTypeId": 189798,
            "h1": "Estados Unidos da América 1 dólar, 2026",
            "subject": "California. Steve Jobs",
        }
        self.assertTrue(details.valid_detail_item(valid, source))

        blocked = {
            **valid,
            "h1": "Grécia 50 cêntimos de euro, 2007 - 2026",
            "subject": "Grécia 50 cêntimos de euro 2007 - 2026",
        }
        self.assertFalse(details.valid_detail_item(blocked, source))

        wrong_tid = {**valid, "ucoinTypeId": 189796}
        self.assertFalse(details.valid_detail_item(wrong_tid, source))

        ui_heading = {
            **valid,
            "documentTitle": (
                "1 dólar 2026 - California. Steve Jobs, "
                "Estados Unidos da América - uCoin.net"
            ),
            "subject": "Notificações",
        }
        self.assertFalse(details.valid_detail_item(ui_heading, source))

    def test_accepts_portugal_and_autonomous_region_pages(self) -> None:
        portugal = {
            "detailUrl": "https://pt.ucoin.net/coin/portugal-5-euro-1998/?tid=76253",
            "ucoinTypeId": 76253,
            "h1": "Portugal 5 euro, 1998",
            "subject": "Expo '98",
        }
        self.assertTrue(
            details.valid_detail_item(
                portugal,
                {"_catalogCountry": "Portugal", "_periodCountry": "Portugal"},
            )
        )

        azores = {
            "detailUrl": "https://pt.ucoin.net/coin/azores-25-escudos-1981/?tid=1",
            "ucoinTypeId": 1,
            "h1": "Região Autónoma dos Açores 25 escudos, 1981",
            "subject": "Autonomia dos Açores",
        }
        self.assertTrue(
            details.valid_detail_item(
                azores,
                {
                    "_catalogCountry": "Portugal",
                    "_periodCountry": "Região Autónoma dos Açores",
                },
            )
        )

    def test_safe_defaults_stop_on_first_block_and_throttle_requests(self) -> None:
        args = details.parse_args(
            ["catalog.json", "--output", "details.json"]
        )

        self.assertEqual(args.retries, 0)
        self.assertEqual(args.delay, 10.0)
        self.assertEqual(args.batch_size, 10)
        self.assertEqual(args.batch_pause, 120.0)

    def test_rate_limit_countdown_is_detected_without_guessing(self) -> None:
        self.assertEqual(
            details.rate_limit_wait(
                "Oops! Unfortunately, it's looks like that your IP is blocked. "
                "Plese wait. 00:09:20"
            ),
            "00:09:20",
        )
        self.assertEqual(details.rate_limit_wait("Portugal 200 escudos"), "")

    def test_reads_urls_that_can_be_preserved_without_another_ucoin_request(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "special-app-catalog.json"
            path.write_text(
                json.dumps(
                    {
                        "programmes": [
                            {
                                "coins": [
                                    {
                                        "detailUrl": "https://pt.ucoin.net/coin/existing/?tid=1"
                                    }
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                details.preserved_detail_urls(path),
                {"https://pt.ucoin.net/coin/existing/?tid=1"},
            )


if __name__ == "__main__":
    unittest.main()
