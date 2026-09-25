from bot.runtime_env import (
    LIVE_EXIT_CANARY_LOCAL_ENV_KEYS, LOSS_EXIT_OPERATOR_LOCAL_ENV_KEYS,
    LOCAL_ENV_KEYS, load_runtime_env,
)


def test_loader_accepts_only_current_outcome_local_surface(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "HL_WALLET_ADDRESS=0xabc\n"
        "OUTCOME_MAX_ENTRY_NOTIONAL_USDC=11\n"
        "DERIBIT_RESEARCH_ENABLED=1\n"
        "DERIBIT_RESEARCH_SNAPSHOT_INTERVAL_SEC=1\n"
        "OUTCOME_RISK_EPISODE_BUDGET_ENABLED=1\n"
        "OUTCOME_NARROW_HARD_FAILURE_CANARY_ENABLED=1\n"
        "OUTCOME_LOSS_EXIT_ENABLED=0\n"
        "OUTCOME_STALE_ENTRY_CANCEL_ENABLED=1\n"
        "OUTCOME_STALE_ENTRY_CANCEL_SEC=60\n"
        "BTC_SPOT_SHADOW_ENABLED=1\n"
        "BTC_SPOT_TESTNET_EXECUTION_ENABLED=0\n"
        "POLYMARKET_PK=must_not_load\n"
        "STRATEGY_PROFILE=must_not_load\n",
        encoding="utf-8",
    )
    environ: dict[str, str] = {}

    load_runtime_env(repo_root=tmp_path, environ=environ)

    assert environ["HL_WALLET_ADDRESS"] == "0xabc"
    assert environ["OUTCOME_MAX_ENTRY_NOTIONAL_USDC"] == "11"
    assert environ["DERIBIT_RESEARCH_ENABLED"] == "1"
    assert environ["DERIBIT_RESEARCH_SNAPSHOT_INTERVAL_SEC"] == "1"
    assert environ["OUTCOME_RISK_EPISODE_BUDGET_ENABLED"] == "1"
    assert environ["OUTCOME_NARROW_HARD_FAILURE_CANARY_ENABLED"] == "1"
    assert environ["OUTCOME_LOSS_EXIT_ENABLED"] == "0"
    assert environ["OUTCOME_STALE_ENTRY_CANCEL_ENABLED"] == "1"
    assert environ["OUTCOME_STALE_ENTRY_CANCEL_SEC"] == "60"
    assert environ["BTC_SPOT_SHADOW_ENABLED"] == "1"
    assert environ["BTC_SPOT_TESTNET_EXECUTION_ENABLED"] == "0"
    assert "POLYMARKET_PK" not in environ
    assert "STRATEGY_PROFILE" not in environ


def test_live_exit_canary_keys_are_part_of_the_local_allowlist():
    """A configured bounded IOC canary must not silently become disabled."""
    assert LIVE_EXIT_CANARY_LOCAL_ENV_KEYS <= LOCAL_ENV_KEYS
    assert LOSS_EXIT_OPERATOR_LOCAL_ENV_KEYS <= LOCAL_ENV_KEYS
    assert {"BTC_SPOT_SHADOW_ENABLED", "BTC_SPOT_TESTNET_EXECUTION_ENABLED",
            "BTC_SPOT_MAX_ORDER_NOTIONAL_USDC", "BTC_SPOT_MAX_POSITION_NOTIONAL_USDC"} <= LOCAL_ENV_KEYS


def test_shell_value_remains_higher_priority_than_local_env(tmp_path):
    (tmp_path / ".env").write_text("OUTCOME_MAX_ENTRY_NOTIONAL_USDC=11\n", encoding="utf-8")
    environ = {"OUTCOME_MAX_ENTRY_NOTIONAL_USDC": "20"}

    load_runtime_env(repo_root=tmp_path, environ=environ)

    assert environ["OUTCOME_MAX_ENTRY_NOTIONAL_USDC"] == "20"
