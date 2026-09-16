from pathlib import Path


def test_modify_order_probe_is_isolated_testnet_and_requires_two_explicit_execution_guards():
    source = (Path(__file__).resolve().parent.parent / "outcome_sdk_sidecar" / "src" / "modify_order_testnet_probe.ts").read_text()
    assert "createHIP4Adapter({ testnet: true })" in source
    assert 'input.execute === true' in source
    assert 'OUTCOME_MODIFY_TESTNET_EXECUTE' in source
    assert "mode: \"dry_run\"" in source
    assert "not part of the long-lived Python sidecar command" in source
