import argparse
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.error import URLError

from scripts import import_base44_coins


class ImportBase44CoinsTests(unittest.TestCase):
    def test_to_coin_record_maps_app_catalogue_fields(self) -> None:
        period = {"title": "Índia › República da Índia › 1957 - 2026"}
        coin = {
            "denomination": "1 naya paisa",
            "issuePeriod": "1957-1961",
            "availability": "withdrawn",
            "detailUrl": "https://pt.ucoin.net/coin/example",
            "obverseImage": "https://example.com/front.jpg",
            "reverseImage": "https://example.com/back.jpg",
        }
        record = import_base44_coins.to_coin_record(
            (period, coin),
            {"country": "Índia", "continent": "Ásia", "condition": "Não Tenho"},
            1,
        )
        self.assertEqual(record["name"], "1 naya paisa")
        self.assertEqual(record["country"], "Índia")
        self.assertEqual(record["continent"], "Ásia")
        self.assertEqual(record["years"], "1957-1961")
        self.assertEqual(record["condition"], "Não Tenho")
        self.assertEqual(record["rarity"], "Retirada")
        self.assertEqual(record["image_frente"], "https://example.com/front.jpg")
        self.assertEqual(record["image_verso"], "https://example.com/back.jpg")
        self.assertEqual(record["url_ucoin"], "https://pt.ucoin.net/coin/example")
        self.assertEqual(record["url_numista"], "")
        self.assertEqual(record["notes"], "República da Índia")
        self.assertEqual(record["ordem"], 1)

    def test_notes_prefer_period_ruler_when_available(self) -> None:
        record = import_base44_coins.to_coin_record(
            ({"title": "Índia › República da Índia › 1957 - 2026", "ruler": "República da Índia"}, {"denomination": "1", "availability": "withdrawn"}),
            {"country": "Índia", "continent": "Ásia", "condition": "Não Tenho"},
            1,
        )
        self.assertEqual(record["notes"], "República da Índia")

    def test_resolve_options_detects_india_continent(self) -> None:
        args = argparse.Namespace(country="", continent="", condition="Não Tenho")
        options = import_base44_coins.resolve_options(args, {"country": "Índia"})
        self.assertEqual(options["continent"], "Ásia")

    def test_unsupported_availability_fails(self) -> None:
        with self.assertRaises(ValueError):
            import_base44_coins.to_coin_record(
                ({}, {"denomination": "1", "availability": "unknown"}),
                {"country": "Índia", "continent": "Ásia", "condition": "Não Tenho"},
                1,
            )

    def test_matching_existing_record_prefers_url_ucoin(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.queries = []

            def filter(self, query, limit=1):
                self.queries.append(query)
                if "url_ucoin" in query:
                    return [{"id": "existing"}]
                return []

        client = FakeClient()
        record = {"country": "Índia", "name": "1 naya paisa", "url_ucoin": "https://pt.ucoin.net/coin/example"}
        self.assertEqual(import_base44_coins.matching_existing_record(client, record), {"id": "existing"})
        self.assertEqual(client.queries[0], {"country": "Índia", "url_ucoin": "https://pt.ucoin.net/coin/example"})

    def test_matching_existing_record_does_not_fallback_to_name_when_url_is_present(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.queries = []

            def filter(self, query, limit=1):
                self.queries.append(query)
                if "url_ucoin" in query:
                    return []
                return [{"id": "same-name-different-years"}]

        client = FakeClient()
        record = {"country": "Índia", "name": "1 naya paisa", "url_ucoin": "https://pt.ucoin.net/coin/new"}
        self.assertIsNone(import_base44_coins.matching_existing_record(client, record))
        self.assertEqual(client.queries, [{"country": "Índia", "url_ucoin": "https://pt.ucoin.net/coin/new"}])

    def test_existing_url_set_uses_only_non_empty_urls(self) -> None:
        urls = import_base44_coins.existing_url_set([
            {"url_ucoin": "https://pt.ucoin.net/coin/a"},
            {"url_ucoin": ""},
            {"name": "missing url"},
        ])
        self.assertEqual(urls, {"https://pt.ucoin.net/coin/a"})

    def test_request_wraps_network_errors(self) -> None:
        client = import_base44_coins.Base44Client(
            "app-id",
            "api-key",
            request_delay_seconds=0,
            rate_limit_delay_seconds=0,
            max_retries=0,
        )
        with patch("scripts.import_base44_coins.urlopen", side_effect=URLError(OSError(101, "Network is unreachable"))):
            with self.assertRaisesRegex(RuntimeError, "Network is unreachable"):
                client.bulk_create([{"name": "5 cêntimos"}])

    def test_create_only_prints_progress_for_each_record(self) -> None:
        class FakeClient:
            def filter(self, query, limit=1):
                return []

            def bulk_create(self, records):
                pass

        records = [
            {"country": "Índia", "name": "1 paisa", "url_ucoin": "https://example.com/1"},
            {"country": "Índia", "name": "2 paisa", "url_ucoin": "https://example.com/2"},
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            imported = import_base44_coins.create_only(FakeClient(), records, allow_duplicates=False)

        self.assertEqual(imported, 2)
        self.assertIn("Created: 1/2 — 1 paisa", output.getvalue())
        self.assertIn("Created: 2/2 — 2 paisa", output.getvalue())

    def test_format_duration_uses_readable_portuguese_style(self) -> None:
        self.assertEqual(import_base44_coins.format_duration(1.75, precise=True), "1,8 s")
        self.assertEqual(import_base44_coins.format_duration(102), "1m 42s")


if __name__ == "__main__":
    unittest.main()
