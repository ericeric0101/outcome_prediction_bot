from bot.outcome_p2_quality import build_p2_capture_quality, is_eligible_p2_snapshot


def _book(timestamp: int):
    return {"time": timestamp, "levels": [[{"px": "0.4", "sz": "10"}], [{"px": "0.6", "sz": "10"}]]}


def test_p2_capture_quality_records_all_clocks_and_accepts_bounded_skew():
    quality = build_p2_capture_quality(
        yes_book=_book(1_000), no_book=_book(1_600),
        yes_local_received_at_ms=1_700, no_local_received_at_ms=1_800, capture_complete_at_ms=1_900,
    )
    assert quality["status"] == "accepted"
    assert quality["side_server_skew_ms"] == 600


def test_p2_capture_quality_rejects_excessive_side_skew():
    quality = build_p2_capture_quality(
        yes_book=_book(1_000), no_book=_book(2_100),
        yes_local_received_at_ms=2_200, no_local_received_at_ms=2_300, capture_complete_at_ms=2_400,
    )
    assert quality["status"] == "rejected"
    assert quality["reason"] == "side_server_skew_exceeded"


def test_eligible_p2_snapshot_requires_current_schema_and_accepted_quality():
    payload = {
        "p2_schema_version": 3, "snapshot_timestamp_ms": 2_000, "fee_evidence": {"status": "observed"},
        "capture_quality": {"status": "accepted"}, "yes_l2": _book(1_800), "no_l2": _book(1_900),
    }
    assert is_eligible_p2_snapshot(payload)
    payload["p2_schema_version"] = 2
    assert not is_eligible_p2_snapshot(payload)
