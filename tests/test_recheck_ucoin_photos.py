from __future__ import annotations

import unittest

from scripts.recheck_ucoin_photos import (
    available_photo_sides,
    canonical_coin_url,
    missing_photo_entries,
)


class RecheckUcoinPhotosTests(unittest.TestCase):
    def test_canonical_coin_url_unwraps_login_reference(self) -> None:
        self.assertEqual(
            canonical_coin_url(
                "https://pt.ucoin.net/login/?ref=/coin/greenland-10-ore-1881/?tid=95665"
            ),
            "https://pt.ucoin.net/coin/greenland-10-ore-1881/?tid=95665",
        )

    def test_missing_photo_entries_collects_each_coin_once(self) -> None:
        payload = [
            {
                "country": "africa-do-sul",
                "country_name": "África do Sul",
                "coins_with_issues": [
                    {
                        "denomination": "10 cêntimos",
                        "issuePeriod": "2026",
                        "ucoinUrl": "https://pt.ucoin.net/coin/south_africa-10-cents-2026/?tid=195420",
                        "issues": [
                            {"type": "missing_image_url", "field": "obverseImage"},
                            {"type": "missing_image_url", "field": "reverseImage"},
                            {"type": "missing_notes", "field": "notes"},
                        ],
                    },
                    {
                        "denomination": "20 cêntimos",
                        "issuePeriod": "2026",
                        "ucoinUrl": "https://pt.ucoin.net/coin/example",
                        "issues": [{"type": "missing_notes", "field": "notes"}],
                    },
                ],
            }
        ]

        self.assertEqual(
            missing_photo_entries(payload),
            [
                {
                    "country": "África do Sul",
                    "denomination": "10 cêntimos",
                    "issuePeriod": "2026",
                    "ucoinUrl": "https://pt.ucoin.net/coin/south_africa-10-cents-2026/?tid=195420",
                    "missingSides": ["frente", "verso"],
                }
            ],
        )

    def test_available_photo_sides_ignores_no_image_placeholder(self) -> None:
        images = [
            {
                "alt": "No Image",
                "src": "https://i.ucoin.net/samples/noimage_s.jpg",
                "href": "https://i.ucoin.net/samples/noimage_s.jpg",
            },
            {
                "alt": "5 cêntimos, Austrália",
                "src": "https://i.ucoin.net/coin/68/910/68910906-1s/australia-5-cents-2024.jpg",
                "href": "https://i.ucoin.net/coin/68/910/68910906-1/australia-5-cents-2024.jpg",
            },
            {
                "alt": "5 cêntimos, Austrália",
                "dataSrc": "https://i.ucoin.net/coin/68/910/68910906-2s/australia-5-cents-2024.jpg",
                "href": "https://i.ucoin.net/coin/68/910/68910906-2/australia-5-cents-2024.jpg",
            },
        ]

        result = available_photo_sides(images)

        self.assertEqual(set(result), {"frente", "verso"})
        self.assertIn("68910906-1", result["frente"])
        self.assertIn("68910906-2", result["verso"])


if __name__ == "__main__":
    unittest.main()
