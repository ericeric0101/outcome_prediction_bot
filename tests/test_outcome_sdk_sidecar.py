import pytest

import bot.outcome_sdk_sidecar as sidecar_module
from bot.outcome_sdk_sidecar import OutcomeSdkAmbiguousExecutionError, OutcomeSdkSidecarClient, OutcomeSdkSidecarError


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


def test_ambiguous_execution_error_carries_only_safe_reconciliation_identifiers():
    error = OutcomeSdkAmbiguousExecutionError(
        command="place_limit_order", request_id="request-1", detail="transport timeout",
    )
    assert error.command == "place_limit_order"
    assert error.request_id == "request-1"
    assert "request-1" in str(error)


def test_execution_transport_timeout_is_ambiguous_not_a_safe_rejection(monkeypatch):
    class Stdin:
        def write(self, _value): pass
        def flush(self): pass

    class Stdout:
        def fileno(self): return 0

    class Process:
        stdin = Stdin()
        stdout = Stdout()
        pid = 123

        def poll(self): return None

    client = OutcomeSdkSidecarClient()
    monkeypatch.setattr(client, "_start", lambda _script: Process())
    monkeypatch.setattr(client, "_discard_unhealthy_process", lambda: None)
    monkeypatch.setattr(sidecar_module.select, "select", lambda *_args: ([], [], []))
    with pytest.raises(OutcomeSdkAmbiguousExecutionError, match="place_limit_order") as caught:
        client.request(
            "place_limit_order",
            payload={"marketId": "1", "outcome": "#10", "side": "buy", "price": "0.6", "amount": "17"},
            allow_execution=True,
        )
    assert caught.value.request_id
