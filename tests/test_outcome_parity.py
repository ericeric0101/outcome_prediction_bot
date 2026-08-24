from datetime import datetime, timezone
from decimal import Decimal

from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec
from bot.outcome_parity import OutcomeParityAnalyzer


def _market():
    return OutcomeMarketSpec(
        outcome_id=516, coin_name="@516", yes_coin="#5160", no_coin="#5161",
        yes_asset_id=100005160, no_asset_id=100005161, market_class="priceBinary",
        underlying="BTC", expiry_str="20260823-0600", expiry_timestamp=int(datetime.now(timezone.utc).timestamp()) + 60,
        start_timestamp=0, target_price=Decimal("70000"), period="15m", raw_spec="",
    )


def test_parity_uses_executable_depth_and_never_claims_fee_adjusted_edge():
    yes = {"levels": [[{"px": "0.49", "sz": "10"}], [{"px": "0.48", "sz": "10"}]]}
    no = {"levels": [[{"px": "0.53", "sz": "10"}], [{"px": "0.50", "sz": "10"}]]}
    result = OutcomeParityAnalyzer(Decimal("10")).analyze(_market(), yes, no)
    assert result.buy_complete_set_cost == Decimal("9.80")
    assert result.buy_complete_set_edge == Decimal("0.20")
    assert result.sell_complete_set_proceeds == Decimal("10.20")
    assert result.sell_complete_set_edge == Decimal("0.20")
    assert result.fee_status == "unverified_excluded"


def test_parity_marks_insufficient_depth_instead_of_inventing_price():
    book = {"levels": [[{"px": "0.49", "sz": "2"}], [{"px": "0.50", "sz": "2"}]]}
    result = OutcomeParityAnalyzer(Decimal("10")).analyze(_market(), book, book)
    assert result.buy_complete_set_cost is None
    assert result.sell_complete_set_proceeds is None


def test_parity_retains_fee_evidence_state_without_claiming_live_trade():
    book = {"levels": [[{"px": "0.51", "sz": "10"}], [{"px": "0.49", "sz": "10"}]]}
    result = OutcomeParityAnalyzer(Decimal("10"), fee_rate=Decimal("0.001")).analyze(_market(), book, book)
    assert result.fee_status == "verified_included"
    assert result.fee_rate == Decimal("0.001")
    assert result.as_dict()["research_only"] is True
