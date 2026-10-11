from __future__ import annotations

import unittest

from scripts import ucoin_detail_subjects as details


class UcoinDetailSubjectTests(unittest.TestCase):
    def test_accepts_only_a_matching_usa_detail_page(self) -> None:
        valid = {
            "detailUrl": "https://pt.ucoin.net/coin/usa-1-dollar-2026/?tid=189798",
            "ucoinTypeId": 189798,
            "h1": "Estados Unidos da América 1 dólar, 2026",
            "subject": "California. Steve Jobs",
        }
        self.assertTrue(details.valid_detail_item(valid))

        blocked = {
            **valid,
            "h1": "Grécia 50 cêntimos de euro, 2007 - 2026",
            "subject": "Grécia 50 cêntimos de euro 2007 - 2026",
        }
        self.assertFalse(details.valid_detail_item(blocked))

        wrong_tid = {**valid, "ucoinTypeId": 189796}
        self.assertFalse(details.valid_detail_item(wrong_tid))

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
