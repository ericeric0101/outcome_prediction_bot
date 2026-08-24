from decimal import Decimal

from bot.outcome_event_bridge import OutcomeFillEvent
from bot.outcome_markout import OutcomeQuote
from bot.outcome_p3_pipeline import OutcomeP3Pipeline
from monitoring.trade_journal_db import TradeJournalDB


def fill():
    return OutcomeFillEvent(1153, 0, "#11530", "c", "1", "tid-1", "BUY", Decimal("0.7"), Decimal("13"), Decimal("0"), "USDH", 1_000, True, {})


def test_p3_pipeline_persists_actual_fill_once_and_records_executable_markouts(tmp_path):
    pipeline = OutcomeP3Pipeline(TradeJournalDB(tmp_path / "p3.db"), "run")
    assert pipeline.record_actual_fill(fill(), period="1d")
    assert not pipeline.record_actual_fill(fill(), period="1d")
    quotes = [OutcomeQuote("#11530", 2_000, Decimal("0.72"), Decimal("0.73")), OutcomeQuote("#11530", 6_000, Decimal("0.71"), Decimal("0.72")), OutcomeQuote("#11530", 11_000, Decimal("0.70"), Decimal("0.71")), OutcomeQuote("#11530", 31_000, Decimal("0.69"), Decimal("0.70"))]
    assert pipeline.observe_quotes(outcome_id=1153, period="1d", quotes=quotes, time_left_sec=700, spread=Decimal("0.01"), depth=Decimal("100"), volatility_regime="unknown") == 4
    assert pipeline.observe_quotes(outcome_id=1153, period="1d", quotes=quotes, time_left_sec=700, spread=Decimal("0.01"), depth=Decimal("100"), volatility_regime="unknown") == 0


def test_p3_pipeline_never_treats_nonmaker_or_unknown_period_as_calibration_sample(tmp_path):
    pipeline = OutcomeP3Pipeline(TradeJournalDB(tmp_path / "p3.db"), "run")
    taker = OutcomeFillEvent(1153, 0, "#11530", None, "1", "tid-t", "BUY", Decimal("0.7"), Decimal("13"), Decimal("0"), "USDH", 1_000, False, {})
    pipeline.record_actual_fill(taker, period="unknown")
    assert pipeline.observe_quotes(outcome_id=1153, period="1d", quotes=[], time_left_sec=700, spread=None, depth=None, volatility_regime="unknown") == 0
