from decimal import Decimal

import pytest

from bot.outcome_maker_cycle import _best_price, whole_shares_for_min_notional


def test_whole_shares_round_up_to_outcome_min_notional():
    assert whole_shares_for_min_notional(Decimal("0.77")) == 13
    assert Decimal("0.77") * whole_shares_for_min_notional(Decimal("0.77")) >= Decimal("10")


def test_best_price_uses_top_of_book_and_rejects_missing_levels():
    assert _best_price([{"price": "0.77", "size": "10"}], "bid") == Decimal("0.77")
    with pytest.raises(RuntimeError, match="no ask"):
        _best_price([], "ask")
