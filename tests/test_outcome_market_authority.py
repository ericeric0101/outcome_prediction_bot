import json

from bot.outcome_market_authority import publish_outcome_market_authority
from bot.lifecycle.outcome_lifecycle import OutcomeMarketSpec


def test_publishes_current_market_as_atomic_downstream_authority(tmp_path):
    market = OutcomeMarketSpec(
        516, "@516", "#5160", "#5161", 100005160, 100005161, "priceBinary", "BTC",
        "20260830-0000", 2_000_000_000, 1, 70000, "1d", "raw",
    )
    path = tmp_path / "outcome_market_authority.json"
    publish_outcome_market_authority(market, path=str(path))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["market_id"] == 516
    assert payload["period"] == "1d"
    assert payload["side0_coin"] == "#5160"
    assert payload["side1_coin"] == "#5161"
    assert payload["updated_at_ms"] > 0


def test_default_authority_path_is_portable_to_the_process_working_directory(tmp_path, monkeypatch):
    market = OutcomeMarketSpec(
        516, "@516", "#5160", "#5161", 100005160, 100005161, "priceBinary", "BTC",
        "20260830-0000", 2_000_000_000, 1, 70000, "1d", "raw",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OUTCOME_MARKET_AUTHORITY_PATH", raising=False)

    publish_outcome_market_authority(market)

    payload = json.loads((tmp_path / "logs" / "outcome_market_authority.json").read_text(encoding="utf-8"))
    assert payload["market_id"] == 516
