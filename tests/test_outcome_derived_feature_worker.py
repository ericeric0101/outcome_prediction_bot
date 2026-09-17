from dataclasses import dataclass

from bot.outcome_derived_feature_worker import OutcomeDerivedFeatureWorker
from monitoring.trade_journal_db import TradeJournalDB


@dataclass
class _Result:
    rows_written: int = 1


def test_derived_worker_is_due_once_and_uses_bounded_write_timeout(monkeypatch, tmp_path):
    calls = []

    class Oi:
        def __init__(self, _journal): pass
        def build(self, **kwargs): calls.append(("oi", kwargs)); return _Result()

    class Deribit:
        def __init__(self, _journal): pass
        def build(self, **kwargs): calls.append(("deribit", kwargs)); return _Result()

    now = [0.0]
    monkeypatch.setattr("bot.outcome_derived_feature_worker.OutcomeOiFeaturePipeline", Oi)
    monkeypatch.setattr("bot.outcome_derived_feature_worker.OutcomeDeribitFeaturePipeline", Deribit)
    worker = OutcomeDerivedFeatureWorker(journal=TradeJournalDB(str(tmp_path / "journal.db")), run_id="test",
                                         interval_sec=60, batch_size=7, write_timeout_sec=0.05,
                                         monotonic=lambda: now[0])
    assert worker.run_once() is True
    assert worker.run_once() is False
    assert [name for name, _ in calls] == ["oi", "deribit"]
    assert all(kwargs["write_timeout_sec"] == 0.05 and kwargs["batch_size"] == 7 for _, kwargs in calls)
