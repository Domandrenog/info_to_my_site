import argparse
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.error import URLError

from scripts import import_base44_coins


class FakeCoinClient:
    def __init__(
        self,
        existing: list[dict[str, object]] | None = None,
        *,
        fail_on_bulk_call: int = 0,
    ) -> None:
        self.existing = list(existing or [])
        self.fail_on_bulk_call = fail_on_bulk_call
        self.filter_calls: list[tuple[dict[str, object], int, int]] = []
        self.bulk_calls: list[list[dict[str, object]]] = []
        self.update_calls: list[tuple[str, dict[str, object]]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.bulk_attempts = 0

    def filter(self, query, limit=1, skip=0):
        self.filter_calls.append((dict(query), limit, skip))
        country = query.get("country")
        matches = [record for record in self.existing if record.get("country") == country]
        return matches[skip : skip + limit]

    def bulk_create(self, records):
        self.bulk_attempts += 1
        if self.fail_on_bulk_call == self.bulk_attempts:
            raise RuntimeError("original create failure")
        self.bulk_calls.append([dict(record) for record in records])

    def update(self, record_id, record):
        self.update_calls.append((record_id, dict(record)))

    def delete_many(self, query):
        self.delete_calls.append(dict(query))


class ImportBase44CoinsTests(unittest.TestCase):
    @staticmethod
    def coin(index: int, *, url: str | None = None) -> dict[str, object]:
        return {
            "country": "Portugal",
            "name": f"{index} euro",
            "years": f"20{index:02d}",
            "url_ucoin": url if url is not None else f"https://pt.ucoin.net/coin/{index}",
        }

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

    def test_notes_prefer_coin_specific_legend(self) -> None:
        record = import_base44_coins.to_coin_record(
            (
                {"title": "África do Sul › República da África do Sul › 1961 - 2026", "ruler": "República da África do Sul"},
                {
                    "denomination": "2 rand",
                    "availability": "circulating",
                    "notes": "República da África do Sul - iSewula Afrika – iNingizimu Afrika",
                },
            ),
            {"country": "África do Sul", "continent": "África", "condition": "Não Tenho"},
            1,
        )
        self.assertEqual(record["notes"], "República da África do Sul - iSewula Afrika – iNingizimu Afrika")

    def test_coin_specific_order_is_preserved_for_a_partial_update(self) -> None:
        record = import_base44_coins.to_coin_record(
            ({}, {"denomination": "2 rand", "availability": "circulating", "ordem": 84}),
            {"country": "África do Sul", "continent": "África", "condition": "Não Tenho"},
            1,
        )
        self.assertEqual(record["ordem"], 84)

    def test_resolve_options_detects_india_continent(self) -> None:
        args = argparse.Namespace(country="", continent="", condition="Não Tenho")
        options = import_base44_coins.resolve_options(args, {"country": "Índia"})
        self.assertEqual(options["continent"], "Ásia")

    def test_resolve_options_detects_existing_country_continent(self) -> None:
        args = argparse.Namespace(country="", continent="", condition="Não Tenho")
        options = import_base44_coins.resolve_options(args, {"country": "Austrália"})
        self.assertEqual(options["continent"], "Oceânia")

    def test_unsupported_availability_fails(self) -> None:
        with self.assertRaises(ValueError):
            import_base44_coins.to_coin_record(
                ({}, {"denomination": "1", "availability": "unknown"}),
                {"country": "Índia", "continent": "Ásia", "condition": "Não Tenho"},
                1,
            )

    def test_classification_uses_only_country_and_ucoin_url(self) -> None:
        record = self.coin(1)
        existing = [{**record, "id": "coin-1", "name": "different", "years": "1900"}]

        plan = import_base44_coins.classify_normal_coin_records([record], existing)

        self.assertEqual(plan.already_existing, [(record, existing[0])])
        self.assertEqual(plan.to_create, [])

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

    def test_a_missing_url_in_base44_is_created_once(self) -> None:
        client = FakeCoinClient()
        record = self.coin(1)
        output = io.StringIO()
        with (
            patch.object(import_base44_coins, "mark_numisvault_stats_stale") as marker,
            redirect_stdout(output),
        ):
            imported = import_base44_coins.create_only(client, [record])

        self.assertEqual(imported, 1)
        self.assertEqual(client.bulk_calls, [[record]])
        self.assertEqual(client.update_calls, [])
        self.assertEqual(client.delete_calls, [])
        marker.assert_called_once()
        self.assertIn("Created: 1/1 — Portugal — 1 euro", output.getvalue())
        self.assertIn("Portugal | 1 euro | 2001 | https://pt.ucoin.net/coin/1", output.getvalue())

    def test_b_single_existing_match_receives_zero_writes_and_is_reported(self) -> None:
        record = self.coin(1)
        client = FakeCoinClient([{**record, "id": "existing-1", "hidden": True}])
        output = io.StringIO()
        with (
            patch.object(import_base44_coins, "mark_numisvault_stats_stale") as marker,
            redirect_stdout(output),
        ):
            imported = import_base44_coins.create_only(client, [record])

        self.assertEqual(imported, 0)
        self.assertEqual(client.bulk_calls, [])
        self.assertEqual(client.update_calls, [])
        self.assertEqual(client.delete_calls, [])
        marker.assert_not_called()
        self.assertIn("ALREADY EXISTED — NOT MODIFIED (1)", output.getvalue())
        self.assertIn("Already existed — untouched: 1", output.getvalue())

    def test_c_missing_input_ucoin_url_is_not_created_and_is_reported(self) -> None:
        record = self.coin(1, url="   ")
        client = FakeCoinClient()
        output = io.StringIO()
        with (
            patch.object(import_base44_coins, "mark_numisvault_stats_stale") as marker,
            redirect_stdout(output),
        ):
            imported = import_base44_coins.create_only(client, [record])

        self.assertEqual(imported, 0)
        self.assertEqual(client.bulk_calls, [])
        marker.assert_not_called()
        self.assertIn("MISSING UCOIN URL — NOT CREATED (1)", output.getvalue())
        self.assertIn("Portugal | 1 euro | 2001", output.getvalue())

    def test_d_multiple_existing_matches_receive_zero_writes_and_are_reported(self) -> None:
        record = self.coin(1)
        existing = [
            {**record, "id": "duplicate-1", "name": "stored one"},
            {**record, "id": "duplicate-2", "name": "stored two"},
        ]
        client = FakeCoinClient(existing)
        output = io.StringIO()
        with (
            patch.object(import_base44_coins, "mark_numisvault_stats_stale") as marker,
            redirect_stdout(output),
        ):
            imported = import_base44_coins.create_only(client, [record])

        self.assertEqual(imported, 0)
        self.assertEqual(client.bulk_calls, [])
        self.assertEqual(client.update_calls, [])
        self.assertEqual(client.delete_calls, [])
        marker.assert_not_called()
        self.assertIn("MULTIPLE EXISTING MATCHES — MANUAL REVIEW (1)", output.getvalue())
        self.assertIn("Base44 ID: duplicate-1 | Name: stored one", output.getvalue())
        self.assertIn("Base44 ID: duplicate-2 | Name: stored two", output.getvalue())

    def test_e_duplicate_input_url_is_created_at_most_once_and_reported(self) -> None:
        first = self.coin(1)
        duplicate = {**first, "name": "duplicate catalogue row"}
        client = FakeCoinClient()
        output = io.StringIO()
        with (
            patch.object(import_base44_coins, "mark_numisvault_stats_stale") as marker,
            redirect_stdout(output),
        ):
            imported = import_base44_coins.create_missing_only(
                client, [first, duplicate], "Portugal", batch_size=10
            )

        self.assertEqual(imported, 1)
        self.assertEqual(client.bulk_calls, [[first]])
        marker.assert_called_once()
        self.assertIn("DUPLICATE INPUT — NOT CREATED AGAIN (1)", output.getvalue())
        self.assertIn("Duplicate input entries: 1", output.getvalue())

    def test_f_95_existing_and_5_missing_create_only_missing_without_put_or_delete(self) -> None:
        records = [self.coin(index) for index in range(1, 101)]
        existing = [{**record, "id": f"existing-{index}"} for index, record in enumerate(records[:95], start=1)]
        client = FakeCoinClient(existing)
        output = io.StringIO()
        with (
            patch.object(import_base44_coins, "mark_numisvault_stats_stale") as marker,
            redirect_stdout(output),
        ):
            imported = import_base44_coins.create_only(client, records)

        self.assertEqual(imported, 5)
        self.assertEqual(len(client.bulk_calls), 5)
        self.assertEqual(client.update_calls, [])
        self.assertEqual(client.delete_calls, [])
        self.assertEqual(len(client.filter_calls), 1)
        marker.assert_called_once()
        self.assertIn("Already existed — untouched: 95", output.getvalue())
        for record in records[:95]:
            self.assertIn(import_base44_coins.coin_report_line(record), output.getvalue())

    def test_g_100_existing_perform_no_writes_and_do_not_mark_adminstats(self) -> None:
        records = [self.coin(index) for index in range(1, 101)]
        existing = [{**record, "id": f"existing-{index}"} for index, record in enumerate(records, start=1)]
        client = FakeCoinClient(existing)
        with (
            patch.object(import_base44_coins, "mark_numisvault_stats_stale") as marker,
            redirect_stdout(io.StringIO()),
        ):
            imported = import_base44_coins.create_missing_only(
                client, records, "Portugal", batch_size=10
            )

        self.assertEqual(imported, 0)
        self.assertEqual(client.bulk_calls, [])
        self.assertEqual(client.update_calls, [])
        self.assertEqual(client.delete_calls, [])
        marker.assert_not_called()

    def test_h_partial_create_failure_marks_adminstats_and_preserves_error(self) -> None:
        records = [self.coin(index) for index in range(1, 4)]
        client = FakeCoinClient(fail_on_bulk_call=2)
        output = io.StringIO()
        with (
            patch.object(import_base44_coins, "mark_numisvault_stats_stale") as marker,
            redirect_stdout(output),
            self.assertRaisesRegex(RuntimeError, "original create failure"),
        ):
            import_base44_coins.create_only(client, records)

        marker.assert_called_once()
        self.assertEqual(client.bulk_calls, [[records[0]]])
        self.assertIn("CREATED (1)", output.getvalue())
        self.assertIn("FAILED CREATES (2)", output.getvalue())

    def test_allow_duplicates_is_explicitly_disabled_before_api_access(self) -> None:
        client = FakeCoinClient()
        with self.assertRaisesRegex(ValueError, "disabled"):
            import_base44_coins.create_only(client, [self.coin(1)], allow_duplicates=True)
        self.assertEqual(client.filter_calls, [])
        self.assertEqual(client.bulk_calls, [])

    def test_format_duration_uses_readable_portuguese_style(self) -> None:
        self.assertEqual(import_base44_coins.format_duration(1.75, precise=True), "1,8 s")
        self.assertEqual(import_base44_coins.format_duration(102), "1m 42s")


if __name__ == "__main__":
    unittest.main()
