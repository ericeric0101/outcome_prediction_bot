from types import SimpleNamespace

import bot.launcher as launcher


def test_live_preflight_fails_closed_when_market_feed_probe_fails(monkeypatch):
    """A caught API exception must not be followed by a live PASSED result."""
    class FailingClient:
        def __init__(self, _auth):
            pass

        def get_outcome_meta_sync(self):
            raise RuntimeError("network unavailable")

    monkeypatch.setattr(launcher, "OutcomeClient", FailingClient)
    monkeypatch.setattr(
        launcher, "resolve_hyperliquid_auth",
        lambda: SimpleNamespace(wallet_address="0xwallet", agent_address="0xagent", is_testnet=False),
    )
    assert launcher.run_hyperliquid_preflight_checks(simulation=False) is False


def test_simulation_preflight_keeps_non_execution_probe_best_effort(monkeypatch):
    class FailingClient:
        def __init__(self, _auth):
            pass

        def get_outcome_meta_sync(self):
            raise RuntimeError("network unavailable")

    monkeypatch.setattr(launcher, "OutcomeClient", FailingClient)
    monkeypatch.setattr(
        launcher, "resolve_hyperliquid_auth",
        lambda: SimpleNamespace(wallet_address="0xwallet", agent_address="0xagent", is_testnet=False),
    )
    assert launcher.run_hyperliquid_preflight_checks(simulation=True) is True
