from bot.outcome_holding_path import OutcomeHoldingPathObservation, OutcomeHoldingPathRecorder
from bot.outcome_holding_path_report import report
from decimal import Decimal
from monitoring.trade_journal_db import TradeJournalDB


def test_report_calculates_path_mae_mfe_and_loss_band_observations(tmp_path):
    journal = TradeJournalDB(tmp_path / "journal.db")
    recorder = OutcomeHoldingPathRecorder(journal, "run")
    for bid in ("0.79", "0.70", "0.85"):
        recorder.record(OutcomeHoldingPathObservation(
            1, "1d", "#10", Decimal("13"), Decimal("0.8"), Decimal(bid),
            Decimal(bid) + Decimal("0.01"), Decimal("0"), 10, 100, "fresh", {},
        ))
    payload = report(journal.db_path)
    path = payload["paths"][0]
    assert path["observations"] == 3
    assert Decimal(path["mae_pct"]) < Decimal("-0.05")
    assert Decimal(path["mfe_pct"]) > 0
