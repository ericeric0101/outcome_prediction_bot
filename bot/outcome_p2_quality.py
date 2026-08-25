"""Versioned integrity rules for durable Outcome P2 book snapshots."""
from __future__ import annotations

from typing import Any, Mapping


P2_SCHEMA_VERSION = 3
MAX_SIDE_SERVER_SKEW_MS = 1_000
MAX_CAPTURE_SERVER_DELTA_MS = 2_000


def _server_time(book: Mapping[str, Any]) -> int | None:
    value = book.get("time")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def build_p2_capture_quality(
    *,
    yes_book: Mapping[str, Any],
    no_book: Mapping[str, Any],
    yes_local_received_at_ms: int,
    no_local_received_at_ms: int,
    capture_complete_at_ms: int,
) -> dict[str, Any]:
    """Record timing facts without pretending sequential REST reads are atomic."""
    yes_server, no_server = _server_time(yes_book), _server_time(no_book)
    quality: dict[str, Any] = {
        "yes_local_received_at_ms": yes_local_received_at_ms,
        "no_local_received_at_ms": no_local_received_at_ms,
        "capture_complete_at_ms": capture_complete_at_ms,
        "yes_server_timestamp_ms": yes_server,
        "no_server_timestamp_ms": no_server,
    }
    if yes_server is None or no_server is None:
        quality.update({"status": "rejected", "reason": "missing_book_server_timestamp"})
        return quality
    side_skew = abs(yes_server - no_server)
    yes_delta = capture_complete_at_ms - yes_server
    no_delta = capture_complete_at_ms - no_server
    quality.update({
        "side_server_skew_ms": side_skew,
        "yes_capture_server_delta_ms": yes_delta,
        "no_capture_server_delta_ms": no_delta,
    })
    if side_skew > MAX_SIDE_SERVER_SKEW_MS:
        quality.update({"status": "rejected", "reason": "side_server_skew_exceeded"})
    elif max(abs(yes_delta), abs(no_delta)) > MAX_CAPTURE_SERVER_DELTA_MS:
        quality.update({"status": "rejected", "reason": "capture_server_delta_exceeded"})
    else:
        quality.update({"status": "accepted", "reason": "within_p2_capture_thresholds"})
    return quality


def is_eligible_p2_snapshot(payload: Mapping[str, Any]) -> bool:
    """Return true only for current, timing-qualified durable P2 evidence."""
    if payload.get("p2_schema_version") != P2_SCHEMA_VERSION:
        return False
    if not isinstance(payload.get("snapshot_timestamp_ms"), int):
        return False
    if not payload.get("fee_evidence"):
        return False
    quality = payload.get("capture_quality")
    if not isinstance(quality, Mapping) or quality.get("status") != "accepted":
        return False
    for key in ("yes_l2", "no_l2"):
        book = payload.get(key)
        if not isinstance(book, Mapping):
            return False
        levels = book.get("levels")
        if not isinstance(levels, list) or len(levels) != 2 or not all(isinstance(side, list) for side in levels):
            return False
    return True
