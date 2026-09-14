from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from ucoin_to_mysite import generate_resume_json


FULL_COIN = {
    "denomination": {"displayName": "5 cêntimos", "value": 5.0, "unit": "cêntimos"},
    "issuePeriod": {"displayValue": "2023-2026", "startYear": 2023, "endYear": 2026},
    "detailUrl": "https://pt.ucoin.net/coin/canada-5-cents-2023-2026/?tid=160035#Info",
    "images": {
        "obverse": "https://i.ucoin.net/coin/70/575/70575318-1s/canada-5-cents-2023.jpg",
        "reverse": "https://i.ucoin.net/coin/70/575/70575318-2s/canada-5-cents-2023.jpg",
    },
}


class GenerateResumeJsonTests(unittest.TestCase):
    def test_to_resume_coin_is_flat_and_has_availability(self) -> None:
        parsed = generate_resume_json.to_resume_coin(FULL_COIN, generate_resume_json.PENDING_AVAILABILITY)
        self.assertEqual(
            parsed,
            {
                "denomination": "5 cêntimos",
                "value": 5.0,
                "unit": "cêntimos",
                "issuePeriod": "2023-2026",
                "startYear": 2023,
                "endYear": 2026,
                "availability": "still needed to calculate",
                "detailUrl": "https://pt.ucoin.net/coin/canada-5-cents-2023-2026/?tid=160035",
                "obverseImage": "https://i.ucoin.net/coin/70/575/70575318-1s/canada-5-cents-2023.jpg",
                "reverseImage": "https://i.ucoin.net/coin/70/575/70575318-2s/canada-5-cents-2023.jpg",
            },
        )
        self.assertNotIn("images", parsed)

    def test_build_resume_catalogue_has_no_design_groups(self) -> None:
        full_catalogue = {
            "country": "Canadá",
            "periods": [
                {
                    "fullTitle": "Canadá › Rei Charles III › 2023 - 2026",
                    "rulerOrPeriodName": "Rei Charles III",
                    "startYear": 2023,
                    "endYear": 2026,
                    "coins": [FULL_COIN],
                }
            ],
        }
        research = {
            "entries": [
                {
                    "detailUrl": "https://pt.ucoin.net/coin/canada-5-cents-2023-2026/?tid=160035",
                    "result": {
                        "availability": "circulating",
                        "belongsToHistoricalSystem": False,
                        "officiallyWithdrawn": False,
                        "reliablyScarce": False,
                    },
                }
            ]
        }
        resume = generate_resume_json.build_resume_catalogue(full_catalogue, research)
        self.assertEqual(resume["country"], "Canadá")
        self.assertNotIn("designs", resume["periods"][0])
        self.assertEqual(resume["periods"][0]["coins"][0]["availability"], "still needed to calculate")
        generate_resume_json.validate_resume_catalogue(resume, 1)

    def test_build_resume_catalogue_can_still_use_research_when_requested(self) -> None:
        full_catalogue = {
            "country": "Canadá",
            "periods": [
                {
                    "fullTitle": "Canadá › Rei Charles III › 2023 - 2026",
                    "rulerOrPeriodName": "Rei Charles III",
                    "startYear": 2023,
                    "endYear": 2026,
                    "coins": [FULL_COIN],
                }
            ],
        }
        research = {
            "entries": [
                {
                    "detailUrl": "https://pt.ucoin.net/coin/canada-5-cents-2023-2026/?tid=160035",
                    "result": {
                        "availability": "circulating",
                        "belongsToHistoricalSystem": False,
                        "officiallyWithdrawn": False,
                        "reliablyScarce": False,
                    },
                }
            ]
        }
        resume = generate_resume_json.build_resume_catalogue(full_catalogue, research, use_research=True)
        self.assertEqual(resume["periods"][0]["coins"][0]["availability"], "circulating")
        generate_resume_json.validate_resume_catalogue(resume, 1)

    def test_build_availability_statistics_uses_issue_years(self) -> None:
        catalogue = {
            "periods": [
                {
                    "coins": [
                        {"availability": "circulating", "startYear": 2023, "endYear": 2026},
                        {"availability": "circulating", "startYear": 1999, "endYear": None},
                        {"availability": "historical", "startYear": None, "endYear": 1949},
                    ]
                }
            ]
        }
        self.assertEqual(
            generate_resume_json.build_availability_statistics(catalogue),
            {
                "availability": {
                    "circulating": {"count": 2, "earliestIssueYear": 1999, "latestIssueYear": 2026},
                    "scarce": {"count": 0, "earliestIssueYear": None, "latestIssueYear": None},
                    "withdrawn": {"count": 0, "earliestIssueYear": None, "latestIssueYear": None},
                    "historical": {"count": 1, "earliestIssueYear": None, "latestIssueYear": 1949},
                }
            },
        )

    def test_statistics_document_has_only_statistics(self) -> None:
        catalogue = {"country": "Canadá", "periods": []}
        result = generate_resume_json.statistics_document(catalogue)
        self.assertEqual(list(result.keys()), ["statistics"])

    def test_without_statistics_keeps_app_catalogue_simple(self) -> None:
        catalogue = {"statistics": {"availability": {}}, "country": "Canadá", "periods": []}
        result = generate_resume_json.without_statistics(catalogue)
        self.assertEqual(result, {"country": "Canadá", "periods": []})

    def test_write_coins_excel_has_required_single_sheet_view(self) -> None:
        catalogue = {
            "country": "Canadá",
            "periods": [
                {
                    "title": "Canadá › Rei Charles III › 2023 - 2026",
                    "coins": [
                        {
                            "denomination": "5 cêntimos",
                            "issuePeriod": "2023-2026",
                            "availability": "circulating",
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "coins-availability.xlsx"
            generate_resume_json.write_coins_excel(str(path), catalogue)
            with zipfile.ZipFile(path) as archive:
                workbook = archive.read("xl/workbook.xml").decode("utf-8")
                sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
                styles = archive.read("xl/styles.xml").decode("utf-8")
        self.assertIn('name="Coins"', workbook)
        self.assertIn('<autoFilter ref="A1:D2"', sheet)
        self.assertIn('state="frozen"', sheet)
        self.assertIn('s="1"', sheet)
        self.assertIn('<b/>', styles)
        self.assertIn('Historical Period', sheet)
        self.assertIn('5 cêntimos', sheet)

    def test_calculate_availability_priority(self) -> None:
        self.assertEqual(
            generate_resume_json.calculate_availability(
                {"belongsToHistoricalSystem": True, "officiallyWithdrawn": True, "reliablyScarce": True}
            ),
            "historical",
        )
        self.assertEqual(
            generate_resume_json.calculate_availability(
                {"belongsToHistoricalSystem": False, "officiallyWithdrawn": True, "reliablyScarce": True}
            ),
            "withdrawn",
        )
        self.assertEqual(
            generate_resume_json.calculate_availability(
                {"belongsToHistoricalSystem": False, "officiallyWithdrawn": False, "reliablyScarce": True}
            ),
            "scarce",
        )

    def test_load_dotenv_loads_missing_values_without_overwriting_environment(self) -> None:
        original_provider = os.environ.get("AI_PROVIDER")
        original_model = os.environ.get("AI_MODEL")
        try:
            os.environ["AI_PROVIDER"] = "already-set"
            os.environ.pop("AI_MODEL", None)
            with tempfile.TemporaryDirectory() as folder:
                env_path = Path(folder) / ".env"
                env_path.write_text('AI_PROVIDER=openai\nAI_MODEL="gpt-test"\n', encoding="utf-8")
                generate_resume_json.load_dotenv(str(env_path))
            self.assertEqual(os.environ["AI_PROVIDER"], "already-set")
            self.assertEqual(os.environ["AI_MODEL"], "gpt-test")
        finally:
            if original_provider is None:
                os.environ.pop("AI_PROVIDER", None)
            else:
                os.environ["AI_PROVIDER"] = original_provider
            if original_model is None:
                os.environ.pop("AI_MODEL", None)
            else:
                os.environ["AI_MODEL"] = original_model

    def test_openai_provider_accepts_openai_alias_environment_names(self) -> None:
        env_keys = (
            "AI_API_KEY",
            "AI_MODEL",
            "AI_BASE_URL",
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
            "OPENAI_API_BASE",
        )
        old_values = {key: os.environ.get(key) for key in env_keys}
        try:
            for key in old_values:
                os.environ.pop(key, None)
            os.environ["OPENAI_API_KEY"] = "test-key"
            os.environ["OPENAI_MODEL"] = "test-model"
            os.environ["OPENAI_API_BASE"] = "https://example.com/serving-endpoints/model/invocations"
            provider = generate_resume_json.OpenAIAvailabilityResearchProvider()
            self.assertEqual(provider.model, "test-model")
            self.assertEqual(provider.base_url, "https://example.com/serving-endpoints/model/invocations")
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


    def test_normalize_detail_url_accepts_markdown_link(self) -> None:
        value = "[https://pt.ucoin.net/coin/belarus-1-kopek-2009/?tid=59333](https://pt.ucoin.net/coin/belarus-1-kopek-2009/?tid=59333)"
        self.assertEqual(
            generate_resume_json.normalize_detail_url(value),
            "https://pt.ucoin.net/coin/belarus-1-kopek-2009/?tid=59333",
        )

    def test_validate_resume_catalogue_normalizes_markdown_detail_url(self) -> None:
        catalogue = {
            "country": "Bielorrussia",
            "periods": [
                {
                    "title": "Bielorrussia",
                    "coins": [
                        {
                            "denomination": "1 kopek",
                            "value": 1.0,
                            "unit": "kopek",
                            "issuePeriod": "2009",
                            "startYear": 2009,
                            "endYear": 2009,
                            "availability": "circulating",
                            "detailUrl": "[https://pt.ucoin.net/coin/belarus-1-kopek-2009/?tid=59333](https://pt.ucoin.net/coin/belarus-1-kopek-2009/?tid=59333)",
                            "obverseImage": "https://i.ucoin.net/coin/1.jpg",
                            "reverseImage": "https://i.ucoin.net/coin/2.jpg",
                        }
                    ],
                }
            ],
        }
        generate_resume_json.validate_resume_catalogue(catalogue, 1)
        self.assertEqual(
            catalogue["periods"][0]["coins"][0]["detailUrl"],
            "https://pt.ucoin.net/coin/belarus-1-kopek-2009/?tid=59333",
        )

    def test_validate_resume_catalogue_normalizes_markdown_image_urls(self) -> None:
        image_url = "https://i.ucoin.net/coin/86/229/86229728-1s/russia-1-kopek-2023.jpg"
        reverse_url = "https://i.ucoin.net/coin/86/229/86229728-2s/russia-1-kopek-2023.jpg"
        catalogue = {
            "country": "Russia",
            "periods": [
                {
                    "title": "Russia",
                    "coins": [
                        {
                            "denomination": "1 kopek",
                            "value": 1.0,
                            "unit": "kopek",
                            "issuePeriod": "1997-2026",
                            "startYear": 1997,
                            "endYear": 2026,
                            "availability": "scarce",
                            "detailUrl": "https://pt.ucoin.net/coin/russia-1-kopek-1997-2026/?tid=1928",
                            "obverseImage": f"[{image_url}]({image_url})",
                            "reverseImage": f"[{reverse_url}]({reverse_url})",
                        }
                    ],
                }
            ],
        }

        generate_resume_json.validate_resume_catalogue(catalogue, 1)

        coin = catalogue["periods"][0]["coins"][0]
        self.assertEqual(coin["obverseImage"], image_url)
        self.assertEqual(coin["reverseImage"], reverse_url)

    def test_validate_resume_catalogue_allows_missing_ucoin_images(self) -> None:
        catalogue = {
            "country": "Nova Zelândia",
            "periods": [
                {
                    "title": "Nova Zelândia",
                    "coins": [
                        {
                            "denomination": "10 cêntimos",
                            "value": 10.0,
                            "unit": "cêntimos",
                            "issuePeriod": "2024",
                            "startYear": 2024,
                            "endYear": 2024,
                            "availability": "still needed to calculate",
                            "detailUrl": "https://pt.ucoin.net/coin/new_zealand-10-cents-2024/?tid=194930",
                            "obverseImage": None,
                            "reverseImage": None,
                        }
                    ],
                }
            ],
        }

        generate_resume_json.validate_resume_catalogue(catalogue, 1)

        coin = catalogue["periods"][0]["coins"][0]
        self.assertEqual(coin["obverseImage"], "")
        self.assertEqual(coin["reverseImage"], "")


if __name__ == "__main__":
    unittest.main()
