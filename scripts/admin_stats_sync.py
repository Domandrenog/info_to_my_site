"""Mark NumisVault aggregate statistics stale after external Base44 writes."""

from __future__ import annotations

import sys
from typing import Any, Callable
from urllib.parse import quote


class AdminStatsSyncError(RuntimeError):
    """Raised when raw data changed but AdminStats could not be marked stale."""


def mark_numisvault_stats_stale(base_client: Any) -> bool:
    """Set only AdminStats.needs_rebuild=true on the canonical record.

    Returns True when a write was required and False when the marker was already
    true.  A missing or ambiguous canonical record is an error; this helper
    never creates AdminStats and never writes false.
    """

    client = base_client.for_entity("AdminStats")
    records = client.filter({}, limit=2)
    if not isinstance(records, list):
        raise AdminStatsSyncError(
            f"Unexpected AdminStats list response: {records!r}"
        )
    if len(records) != 1:
        raise AdminStatsSyncError(
            f"Expected exactly one canonical AdminStats record; found {len(records)}."
        )

    current = records[0]
    if not isinstance(current, dict):
        raise AdminStatsSyncError("Canonical AdminStats response is not an object.")
    if current.get("needs_rebuild") is True:
        print("NumisVault AdminStats already marked for rebuild.")
        return False

    record_id = str(current.get("id") or "").strip()
    if not record_id:
        raise AdminStatsSyncError("Canonical AdminStats record has no id.")

    client.update(record_id, {"needs_rebuild": True})
    verified = client.request("GET", f"{client.base_url}/{quote(record_id)}")
    if not isinstance(verified, dict) or verified.get("needs_rebuild") is not True:
        raise AdminStatsSyncError(
            "AdminStats update returned without confirming needs_rebuild=true."
        )

    print("NumisVault AdminStats marked for rebuild.")
    return True


class MutationRun:
    """Track successful raw mutations and mark AdminStats once per operation."""

    def __init__(
        self,
        marker: Callable[[], bool],
        *,
        mutation_label: str = "Base44 records",
    ) -> None:
        self._marker = marker
        self.mutation_label = mutation_label
        self.mutation_count = 0

    def __enter__(self) -> "MutationRun":
        return self

    def record_success(self, count: int = 1) -> None:
        if count < 1:
            return
        self.mutation_count += count

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> bool:
        if self.mutation_count == 0:
            return False

        print(
            f"Base44: {self.mutation_count} successful "
            f"{self.mutation_label} mutation(s)."
        )
        try:
            self._marker()
        except Exception as marker_error:  # noqa: BLE001 - preserve original failure
            message = (
                "CRITICAL: Base44 data was modified successfully, but NumisVault "
                "AdminStats could not be marked for rebuild."
            )
            print(message, file=sys.stderr)
            print(f"AdminStats marker error: {marker_error}", file=sys.stderr)
            if exc is None:
                raise AdminStatsSyncError(message) from marker_error
        return False
