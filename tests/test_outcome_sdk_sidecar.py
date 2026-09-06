import pytest

from bot.outcome_sdk_sidecar import OutcomeSdkSidecarClient, OutcomeSdkSidecarError


def test_sidecar_client_requires_explicit_execution_opt_in_before_subprocess():
    client = OutcomeSdkSidecarClient("missing-sidecar")
    with pytest.raises(OutcomeSdkSidecarError, match="explicit allow_execution"):
        client.request("place_limit_order")  # type: ignore[arg-type]


def test_sidecar_client_requires_a_built_sidecar_for_read_only_calls():
    client = OutcomeSdkSidecarClient("missing-sidecar")
    with pytest.raises(OutcomeSdkSidecarError, match="not built"):
        client.request("health")


def test_sidecar_client_reads_health_protocol_from_a_built_sidecar():
    with OutcomeSdkSidecarClient() as client:
        result = client.request("health")
        assert result["execution"] == "disabled_by_default"


def test_sidecar_reuses_one_long_lived_node_process_for_multiple_requests():
    with OutcomeSdkSidecarClient() as client:
        client.request("health")
        first = dict(client.last_request_timing or {})
        client.request("health")
        second = dict(client.last_request_timing or {})
    assert first["persistent"] is True
    assert first["pid"] == second["pid"]
    assert second["command"] == "health"


def test_sidecar_client_does_not_allow_execution_without_the_sidecar_gate():
    client = OutcomeSdkSidecarClient()
    with pytest.raises(OutcomeSdkSidecarError, match="EXECUTION_DISABLED"):
        client.request(
            "cancel_order",
            payload={"marketId": "1153", "outcome": "#11530", "orderId": "1"},
            allow_execution=True,
        )


def test_sidecar_client_emergency_ioc_still_requires_execution_opt_in():
    client = OutcomeSdkSidecarClient("missing-sidecar")
    with pytest.raises(OutcomeSdkSidecarError, match="explicit allow_execution"):
        client.request("place_emergency_ioc_exit")  # type: ignore[arg-type]
