import pytest

from bot.outcome_sdk_sidecar import OutcomeSdkSidecarClient, OutcomeSdkSidecarError


def test_sidecar_client_rejects_execution_commands_before_subprocess():
    client = OutcomeSdkSidecarClient("missing-sidecar")
    with pytest.raises(OutcomeSdkSidecarError, match="P4 hard-disabled"):
        client.request("place_limit_order")  # type: ignore[arg-type]


def test_sidecar_client_requires_a_built_sidecar_for_read_only_calls():
    client = OutcomeSdkSidecarClient("missing-sidecar")
    with pytest.raises(OutcomeSdkSidecarError, match="not built"):
        client.request("health")


def test_sidecar_client_reads_health_protocol_from_a_built_sidecar():
    client = OutcomeSdkSidecarClient()
    result = client.request("health")
    assert result["execution"] == "hard_disabled"
