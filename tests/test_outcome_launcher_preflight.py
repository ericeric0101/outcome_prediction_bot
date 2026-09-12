from types import SimpleNamespace
import signal
import threading

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


def test_sigterm_handler_requests_orderly_shutdown_and_is_restorable():
    stop_event = threading.Event()
    previous = signal.getsignal(signal.SIGTERM)
    saved = launcher.install_graceful_shutdown_handler(stop_event)
    try:
        assert saved is not None
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert stop_event.is_set()
    finally:
        launcher.restore_signal_handler(saved)
    assert signal.getsignal(signal.SIGTERM) == previous
