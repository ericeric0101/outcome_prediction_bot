from bot.runtime_env import load_runtime_env


def test_loader_accepts_only_current_outcome_local_surface(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "HL_WALLET_ADDRESS=0xabc\n"
        "OUTCOME_MAX_ENTRY_NOTIONAL_USDC=11\n"
        "POLYMARKET_PK=must_not_load\n"
        "STRATEGY_PROFILE=must_not_load\n",
        encoding="utf-8",
    )
    environ: dict[str, str] = {}

    load_runtime_env(repo_root=tmp_path, environ=environ)

    assert environ["HL_WALLET_ADDRESS"] == "0xabc"
    assert environ["OUTCOME_MAX_ENTRY_NOTIONAL_USDC"] == "11"
    assert "POLYMARKET_PK" not in environ
    assert "STRATEGY_PROFILE" not in environ


def test_shell_value_remains_higher_priority_than_local_env(tmp_path):
    (tmp_path / ".env").write_text("OUTCOME_MAX_ENTRY_NOTIONAL_USDC=11\n", encoding="utf-8")
    environ = {"OUTCOME_MAX_ENTRY_NOTIONAL_USDC": "20"}

    load_runtime_env(repo_root=tmp_path, environ=environ)

    assert environ["OUTCOME_MAX_ENTRY_NOTIONAL_USDC"] == "20"
