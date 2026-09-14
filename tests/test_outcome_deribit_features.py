import json
import sqlite3

import pytest

from bot.outcome_deribit_features import DERIBIT_FEATURE_SCHEMA_VERSION, OutcomeDeribitFeaturePipeline
from bot.outcome_p2_quality import P2_SCHEMA_VERSION
from monitoring.trade_journal_db import TradeJournalDB


def _book(bid: str, ask: str) -> dict:
    return {"levels": [[{"px": bid, "sz": "10"}], [{"px": ask, "sz": "10"}]]}


def _outcome(timestamp: int, *, bid: str = "0.40", ask: str = "0.42", market: int = 9) -> dict:
    return {
        "period": "1d", "p2_schema_version": P2_SCHEMA_VERSION, "snapshot_timestamp_ms": timestamp,
        "outcome_id": market, "time_left_sec": 50_000, "strike": "70000",
        "yes_l2": _book(bid, ask), "no_l2": _book("0.58", "0.60"),
        "capture_quality": {"status": "accepted"}, "fee_evidence": {"status": "unverified"},
    }


def _deribit(local: int, *, valid: bool = True, mid: float = 70_000.0) -> dict:
    return {
        "valid": valid, "source": "deribit_public_ws", "source_timestamp_ms": local - 100,
        "local_received_at_ms": local, "index_price": mid - 1, "mid": mid,
        "spread_bps": 1.0, "top_imbalance": 0.2, "mark_price": mid + 1,
        "open_interest": 1_000_000, "funding_8h": 0.0001,
        "trade_flow_imbalance_1s": 0.1,
    }


def test_d2_joins_only_deribit_locally_known_at_outcome_decision(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    base = 2_000_000_000_000
    first_deribit_id = journal.log_strategy_event("deribit", "DERIBIT_FEATURE_SNAPSHOT", _deribit(base - 1, mid=70_000))
    first_outcome_id = journal.log_strategy_event("outcome", "OUTCOME_P2_PARITY_SNAPSHOT", _outcome(base))
    journal.log_strategy_event("outcome", "OUTCOME_P2_PARITY_SNAPSHOT", _outcome(base + 5_000, bid="0.50", ask="0.52"))
    journal.log_strategy_event("deribit", "DERIBIT_FEATURE_SNAPSHOT", _deribit(base + 1, mid=99_000))
    result = OutcomeDeribitFeaturePipeline(journal).build()
    assert result.eligible_outcome_snapshots == 2
    assert result.deribit_joined == 2
    assert result.labels_available[5] == 1
    with sqlite3.connect(journal.db_path) as conn:
        row = conn.execute(
            "SELECT deribit_snapshot_event_id,deribit_local_received_at_ms,features_json,labels_json "
            "FROM outcome_deribit_feature_rows WHERE outcome_snapshot_event_id=?", (first_outcome_id,)
        ).fetchone()
    assert row[0] == first_deribit_id
    assert row[1] == base - 1
    features, labels = json.loads(row[2]), json.loads(row[3])
    assert features["deribit_mid"] == 70_000.0
    assert labels["future_5s"]["yes_long_markout_ps"] == pytest.approx(0.08)


def test_d2_rejects_invalid_deribit_snapshot_without_imputation(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    base = 2_100_000_000_000
    journal.log_strategy_event("deribit", "DERIBIT_FEATURE_SNAPSHOT", _deribit(base - 1, valid=False))
    journal.log_strategy_event("outcome", "OUTCOME_P2_PARITY_SNAPSHOT", _outcome(base))
    result = OutcomeDeribitFeaturePipeline(journal).build()
    # There is no valid Deribit evidence, so the builder preserves the
    # Outcome row as unavailable rather than inventing a joined source row.
    assert result.eligible_outcome_snapshots == 1
    assert result.deribit_joined == 0
    assert result.deribit_unavailable == 1
