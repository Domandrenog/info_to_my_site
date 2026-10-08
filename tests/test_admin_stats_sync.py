from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from scripts.admin_stats_sync import (
    AdminStatsSyncError,
    MutationRun,
    mark_numisvault_stats_stale,
)


class FakeAdminStatsClient:
    base_url = "https://base44.test/entities/AdminStats"

    def __init__(self, records: list[dict[str, object]]) -> None:
        self.records = records
        self.updates: list[tuple[str, dict[str, object]]] = []

    def filter(self, query, limit=1):
        self.last_filter = (query, limit)
        return [dict(record) for record in self.records]

    def update(self, record_id, payload):
        self.updates.append((record_id, dict(payload)))
        for record in self.records:
            if record.get("id") == record_id:
                record.update(payload)

    def request(self, method, url):
        return dict(self.records[0])


class FakeBaseClient:
    def __init__(self, admin_client: FakeAdminStatsClient) -> None:
        self.admin_client = admin_client
        self.entity_requests: list[str] = []

    def for_entity(self, entity_name):
        self.entity_requests.append(entity_name)
        return self.admin_client


class AdminStatsSyncTests(unittest.TestCase):
    def test_marker_uses_one_canonical_record_and_partial_put(self) -> None:
        admin = FakeAdminStatsClient([{"id": "stats-1", "needs_rebuild": False, "total": 9}])
        base = FakeBaseClient(admin)

        with redirect_stdout(io.StringIO()):
            changed = mark_numisvault_stats_stale(base)

        self.assertTrue(changed)
        self.assertEqual(base.entity_requests, ["AdminStats"])
        self.assertEqual(admin.last_filter, ({}, 2))
        self.assertEqual(admin.updates, [("stats-1", {"needs_rebuild": True})])

    def test_marker_does_not_write_when_already_true(self) -> None:
        admin = FakeAdminStatsClient([{"id": "stats-1", "needs_rebuild": True}])

        with redirect_stdout(io.StringIO()):
            changed = mark_numisvault_stats_stale(FakeBaseClient(admin))

        self.assertFalse(changed)
        self.assertEqual(admin.updates, [])

    def test_zero_successful_mutations_do_not_mark(self) -> None:
        calls: list[str] = []
        with MutationRun(lambda: calls.append("marked")):
            pass
        self.assertEqual(calls, [])

    def test_one_or_multiple_mutations_mark_exactly_once(self) -> None:
        for count in (1, 4):
            calls: list[str] = []
            with redirect_stdout(io.StringIO()):
                with MutationRun(lambda: calls.append("marked")) as run:
                    run.record_success(count)
            self.assertEqual(calls, ["marked"])

    def test_partial_failure_still_marks_and_preserves_original_error(self) -> None:
        calls: list[str] = []
        with self.assertRaisesRegex(ValueError, "original"):
            with redirect_stdout(io.StringIO()):
                with MutationRun(lambda: calls.append("marked")) as run:
                    run.record_success()
                    raise ValueError("original")
        self.assertEqual(calls, ["marked"])

    def test_failure_before_first_mutation_does_not_mark(self) -> None:
        calls: list[str] = []
        with self.assertRaisesRegex(ValueError, "before"):
            with MutationRun(lambda: calls.append("marked")):
                raise ValueError("before")
        self.assertEqual(calls, [])

    def test_marker_failure_is_non_successful_without_original_error(self) -> None:
        def fail_marker() -> None:
            raise RuntimeError("marker unavailable")

        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(AdminStatsSyncError),
        ):
            with MutationRun(fail_marker) as run:
                run.record_success()
        self.assertIn("CRITICAL", stderr.getvalue())

    def test_marker_failure_keeps_original_failure_visible(self) -> None:
        def fail_marker() -> None:
            raise RuntimeError("marker unavailable")

        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as stderr,
            self.assertRaisesRegex(ValueError, "original"),
        ):
            with MutationRun(fail_marker) as run:
                run.record_success()
                raise ValueError("original")
        self.assertIn("CRITICAL", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
