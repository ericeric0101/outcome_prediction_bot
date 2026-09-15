from bot.runtime_env import LIVE_EXIT_CANARY_LOCAL_ENV_KEYS, LOCAL_ENV_KEYS, load_runtime_env


def test_loader_accepts_only_current_outcome_local_surface(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "HL_WALLET_ADDRESS=0xabc\n"
        "OUTCOME_MAX_ENTRY_NOTIONAL_USDC=11\n"
        "DERIBIT_RESEARCH_ENABLED=1\n"
        "DERIBIT_RESEARCH_SNAPSHOT_INTERVAL_SEC=1\n"
        "OUTCOME_RISK_EPISODE_BUDGET_ENABLED=1\n"
        "OUTCOME_NARROW_HARD_FAILURE_CANARY_ENABLED=1\n"
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
    assert "POLYMARKET_PK" not in environ
    assert "STRATEGY_PROFILE" not in environ


def test_live_exit_canary_keys_are_part_of_the_local_allowlist():
    """A configured bounded IOC canary must not silently become disabled."""
    assert LIVE_EXIT_CANARY_LOCAL_ENV_KEYS <= LOCAL_ENV_KEYS


def test_shell_value_remains_higher_priority_than_local_env(tmp_path):
    (tmp_path / ".env").write_text("OUTCOME_MAX_ENTRY_NOTIONAL_USDC=11\n", encoding="utf-8")
    environ = {"OUTCOME_MAX_ENTRY_NOTIONAL_USDC": "20"}

    load_runtime_env(repo_root=tmp_path, environ=environ)

    assert environ["OUTCOME_MAX_ENTRY_NOTIONAL_USDC"] == "20"
