import json
import sqlite3

from bot.outcome_research_gate import OutcomeResearchGate


def test_research_gate_blocks_when_p2_or_p3_has_no_accepted_evidence(tmp_path):
    db = tmp_path / "journal.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE strategy_events (event_type TEXT, payload_json TEXT)")
        conn.execute("CREATE TABLE order_events (event_type TEXT, payload_json TEXT)")
    result = OutcomeResearchGate(str(db)).check("15m")
    assert not result.allowed
    assert result.reason.startswith("P2 research gate:")


def test_research_gate_requires_every_p3_horizon_to_pass(tmp_path):
    db = tmp_path / "journal.db"
    snapshot = {
        "venue": "hyperliquid_outcome", "p2_schema_version": 3, "period": "15m",
        "snapshot_timestamp_ms": 1, "yes_l2": {"levels": [[], []]}, "no_l2": {"levels": [[], []]},
        "capture_quality": {"status": "accepted"}, "fee_evidence": {"official": True},
        "fee_status": "verified_included", "buy_complete_set_cost": 9.9,
        "buy_complete_set_edge": 0.1, "sell_complete_set_proceeds": 10.1,
    }
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE strategy_events (event_type TEXT, payload_json TEXT)")
        conn.execute("CREATE TABLE order_events (event_type TEXT, payload_json TEXT)")
        for _ in range(100):
            conn.execute("INSERT INTO strategy_events VALUES (?, ?)", ("OUTCOME_P2_PARITY_SNAPSHOT", json.dumps(snapshot)))
    result = OutcomeResearchGate(str(db)).check("15m")
    assert not result.allowed
    assert result.reason.startswith("P3 research gate:")
