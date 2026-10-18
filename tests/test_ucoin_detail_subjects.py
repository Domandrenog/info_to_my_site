from __future__ import annotations

import unittest

from scripts import ucoin_detail_subjects as details


class UcoinDetailSubjectTests(unittest.TestCase):
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
        self.assertEqual(args.delay, 5.0)
        self.assertEqual(args.batch_size, 20)
        self.assertEqual(args.batch_pause, 60.0)


if __name__ == "__main__":
    unittest.main()
