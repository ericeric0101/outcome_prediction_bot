from bot.outcome_open_orders_stream import OutcomeOpenOrdersStreamCache


WALLET = "0x" + "a" * 40


def _order(oid: int = 7, *, price: str = "0.60") -> dict[str, object]:
    return {"oid": oid, "coin": "#1230", "side": "B", "sz": "20", "limitPx": price}


def _message(orders, *, user: str = WALLET):
    return {"channel": "openOrders", "data": {"user": user, "dex": "ALL_DEXS", "orders": orders}}


def test_stream_requires_same_connection_rest_cross_validation():
    stream = OutcomeOpenOrdersStreamCache(WALLET, rest_reconcile_interval_sec=60)
    assert stream.on_message(_message([_order()])) is False
    stream.on_lifecycle("connected")
    assert stream.on_message(_message([_order()])) is True
    assert stream.trusted_orders(WALLET, now=10) is None
    assert stream.mark_rest_verified(WALLET, [_order()], now=10) is True
    assert stream.trusted_orders(WALLET, now=69) == [_order()]
    assert stream.trusted_orders(WALLET, now=70) is None


def test_verified_stream_tracks_later_full_snapshots_without_more_rest():
    stream = OutcomeOpenOrdersStreamCache(WALLET)
    stream.on_lifecycle("connected")
    stream.on_message(_message([_order()]))
    assert stream.mark_rest_verified(WALLET, [_order()], now=10)
    stream.on_message(_message([_order(8, price="0.61")]))
    assert stream.trusted_orders(WALLET, now=11) == [_order(8, price="0.61")]


def test_mismatch_mutation_disconnect_and_reconnect_all_fail_closed():
    stream = OutcomeOpenOrdersStreamCache(WALLET)
    stream.on_lifecycle("connected")
    stream.on_message(_message([_order()]))
    assert stream.mark_rest_verified(WALLET, [_order(price="0.59")], now=10) is False
    assert stream.trusted_orders(WALLET, now=11) is None

    assert stream.mark_rest_verified(WALLET, [_order()], now=12)
    stream.invalidate_for_mutation()
    assert stream.trusted_orders(WALLET, now=13) is None

    stream.on_lifecycle("disconnected")
    stream.on_lifecycle("connected")
    assert stream.trusted_orders(WALLET, now=14) is None
    assert stream.on_message(_message([_order()], user="0x" + "b" * 40)) is False
    assert stream.on_message({"data": {"user": WALLET, "orders": [{"oid": 1}]}}) is False


def test_empty_snapshot_can_be_verified_but_unrelated_lifecycle_is_ignored():
    stream = OutcomeOpenOrdersStreamCache(WALLET)
    stream.on_lifecycle("connected")
    assert stream.on_message(_message([]))
    assert stream.mark_rest_verified(WALLET, [], now=10)
    stream.on_lifecycle("subscribed")
    assert stream.trusted_orders(WALLET, now=11) == []
