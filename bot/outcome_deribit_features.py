"""D2: leak-free as-of joins between Outcome P2 snapshots and Deribit data.

This is an offline research builder.  It cannot import a client, runtime,
gateway, or execution service.  A Deribit observation is eligible only when
it was locally received no later than the Outcome snapshot and its own
collector already declared the book valid.
"""
from __future__ import annotations

import json
import math
import sqlite3
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from bot.outcome_oi_features import _bbo
from bot.outcome_p2_quality import is_eligible_p2_snapshot
from monitoring.db_mem_diag import DbMemDiag
from monitoring.trade_journal_db import TradeJournalDB


# v2 adds a fixed, persisted as-of freshness budget.  Do not read the current
# environment here: a historical research row must not change validity when a
# later deployment adjusts its collector setting.
DERIBIT_FEATURE_SCHEMA_VERSION = 2
DERIBIT_MAX_JOIN_AGE_MS = 3_000
DERIBIT_LABEL_HORIZONS_SEC = (5, 15, 30, 60, 300)
DERIBIT_LABEL_TOLERANCE_MS = 7_500


def _number(value: Any) -> float | None:
    try:
        result = float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass(frozen=True)
class _DeribitPoint:
    event_id: int
    source_timestamp_ms: int | None
    local_received_at_ms: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class D2BuildResult:
    eligible_outcome_snapshots: int
    rows_written: int
    deribit_joined: int
    deribit_unavailable: int
    deribit_stale_rejected: int
    labels_available: dict[int, int]
    first_deribit_received_at_ms: int | None
    last_deribit_received_at_ms: int | None


class _DeribitIndex:
    def __init__(self, points: list[_DeribitPoint]) -> None:
        self.points = tuple(sorted(points, key=lambda point: point.local_received_at_ms))
        self.times = tuple(point.local_received_at_ms for point in self.points)

    def as_of(self, timestamp_ms: int) -> _DeribitPoint | None:
        index = bisect_right(self.times, timestamp_ms) - 1
        return self.points[index] if index >= 0 else None

    def return_bps(self, point: _DeribitPoint, *, field: str, horizon_sec: int) -> float | None:
        prior = self.as_of(point.local_received_at_ms - horizon_sec * 1000)
        current_value = _number(point.payload.get(field))
        prior_value = _number(prior.payload.get(field)) if prior else None
        if current_value is None or prior_value is None or prior_value <= 0:
            return None
        return ((current_value / prior_value) - 1.0) * 10_000.0


class OutcomeDeribitFeaturePipeline:
    """Build recomputable D2 rows with strict local-receipt as-of semantics."""

    def __init__(self, journal: TradeJournalDB) -> None:
        self.journal = journal

    @staticmethod
    def _valid_point(event_id: int, raw: object) -> _DeribitPoint | None:
        try:
            payload = json.loads(raw or "{}")
            local = int(payload.get("local_received_at_ms"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("valid") is not True:
            return None
        source = payload.get("source_timestamp_ms")
        try:
            source_ms = int(source) if source is not None else None
        except (TypeError, ValueError):
            return None
        # Collector's immutable declaration means the book was continuous
        # and fresh. Reject pathological clocks instead of repairing.
        if source_ms is not None and source_ms > local:
            return None
        return _DeribitPoint(int(event_id), source_ms, local, payload)

    def _sync_point_index(self, *, write_timeout_sec: float) -> None:
        """Stream newly appended valid Deribit source rows into an indexed mirror.

        The cache is derived research data.  A failed short write simply makes
        the worker retry later; it cannot affect execution or journal truth.
        """
        diag = DbMemDiag("outcome_deribit_point_index_sync")
        row_count = payload_bytes = 0
        try:
            # The cache is first created with a small number of bounded
            # transactions. The regular 50ms research-write budget can miss
            # every batch while telemetry is active, so allow this
            # background-only path a modest lock-acquisition window; each
            # 5k-row batch still commits immediately.
            with sqlite3.connect(self.journal.db_path, timeout=max(write_timeout_sec, 5.0)) as conn:
                checkpoint = conn.execute(
                    "SELECT last_source_event_id FROM outcome_deribit_point_index_state_v2 WHERE singleton=1"
                ).fetchone()
                last_event_id = int(checkpoint[0]) if checkpoint is not None else 0
                cursor = conn.execute(
                    """SELECT id,payload_json FROM strategy_events
                       WHERE event_type='DERIBIT_FEATURE_SNAPSHOT' AND id>?
                       ORDER BY id""",
                    (last_event_id,),
                )
                batch: list[tuple[int, int | None, int, str]] = []
                pending_watermark = last_event_id
                pending_source_rows = 0

                def commit_batch() -> None:
                    nonlocal pending_source_rows
                    if batch:
                        conn.executemany(
                            """INSERT OR REPLACE INTO outcome_deribit_point_index_v2
                               (source_event_id,source_timestamp_ms,local_received_at_ms,payload_json)
                               VALUES (?,?,?,?)""",
                            batch,
                        )
                    conn.execute(
                        """INSERT INTO outcome_deribit_point_index_state_v2(singleton,last_source_event_id)
                           VALUES (1,?)
                           ON CONFLICT(singleton) DO UPDATE SET last_source_event_id=excluded.last_source_event_id""",
                        (pending_watermark,),
                    )
                    conn.commit()
                    batch.clear()
                    pending_source_rows = 0

                for event_id, raw in cursor:
                    row_count += 1
                    pending_source_rows += 1
                    pending_watermark = int(event_id)
                    if raw is not None:
                        payload_bytes += len(str(raw).encode("utf-8"))
                    point = self._valid_point(int(event_id), raw)
                    if point is not None:
                        batch.append((point.event_id, point.source_timestamp_ms, point.local_received_at_ms, str(raw)))
                    if pending_source_rows >= 5_000:
                        commit_batch()
                if pending_source_rows:
                    commit_batch()
            diag.after_query(rows=row_count, payload_bytes=payload_bytes)
        finally:
            diag.finish(note="streamed_incremental_point_cache")

    def _outcome_snapshots(
        self,
        conn: sqlite3.Connection, *, after_ms: int | None, after_event_id: int | None,
        checkpoint_event_id: int | None = None, refresh_after_ms: int | None = None,
    ) -> list[tuple[int, dict[str, Any]]]:
        # The journal id is only an efficient lower bound on scanning; it is
        # never used as timing evidence.  The actual join still requires
        # Deribit local receipt <= Outcome snapshot timestamp.
        diag = DbMemDiag("outcome_deribit_feature_outcome_snapshots")
        snapshots: list[tuple[int, dict[str, Any]]] = []
        row_count = payload_bytes = 0
        try:
            sql = "SELECT id,payload_json FROM strategy_events WHERE event_type='OUTCOME_P2_PARITY_SNAPSHOT'"
            params: tuple[int, ...] = ()
            if checkpoint_event_id is not None and refresh_after_ms is not None:
                sql += """ AND (id > ? OR id IN (
                    SELECT outcome_snapshot_event_id
                    FROM outcome_deribit_feature_rows
                    WHERE feature_schema_version=? AND snapshot_timestamp_ms>=?
                ))"""
                params = (checkpoint_event_id, DERIBIT_FEATURE_SCHEMA_VERSION, refresh_after_ms)
            elif after_event_id is not None:
                sql += " AND id>=?"
                params = (after_event_id,)
            for event_id, raw in conn.execute(sql + " ORDER BY id", params):
                row_count += 1
                if raw is not None:
                    payload_bytes += len(str(raw).encode("utf-8"))
                try:
                    payload = json.loads(raw or "{}")
                    timestamp = int(payload.get("snapshot_timestamp_ms"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(payload, dict) and payload.get("period") == "1d"
                    and is_eligible_p2_snapshot(payload) and (after_ms is None or timestamp >= after_ms)
                ):
                    snapshots.append((int(event_id), payload))
            diag.after_query(rows=row_count, payload_bytes=payload_bytes)
            return snapshots
        finally:
            diag.finish(note=(
                "incremental_new_plus_label_tail"
                if checkpoint_event_id is not None else "full_history_streamed"
            ))

    @classmethod
    def _deribit_points(
        cls, conn: sqlite3.Connection, *, lower_ms: int | None = None, upper_ms: int | None = None,
    ) -> list[_DeribitPoint]:
        diag = DbMemDiag("outcome_deribit_feature_points")
        points: list[_DeribitPoint] = []
        row_count = payload_bytes = 0
        try:
            if lower_ms is None or upper_ms is None:
                cursor = conn.execute(
                    """SELECT source_event_id,payload_json FROM outcome_deribit_point_index_v2
                       ORDER BY local_received_at_ms,source_event_id"""
                )
            else:
                # Include one point before the feature window. It determines
                # the exact stale/unavailable decision if no fresh point is
                # present, while the 63-second lookback covers all returns.
                cursor = conn.execute(
                    """WITH window_points AS (
                         SELECT source_event_id,payload_json FROM outcome_deribit_point_index_v2
                         WHERE local_received_at_ms>=? AND local_received_at_ms<=?
                         UNION ALL
                         SELECT source_event_id,payload_json FROM outcome_deribit_point_index_v2
                         WHERE source_event_id=(
                           SELECT source_event_id FROM outcome_deribit_point_index_v2
                           WHERE local_received_at_ms<?
                           ORDER BY local_received_at_ms DESC,source_event_id DESC LIMIT 1
                         )
                       )
                       SELECT source_event_id,payload_json FROM window_points
                       ORDER BY source_event_id""",
                    (lower_ms, upper_ms, lower_ms),
                )
            for event_id, raw in cursor:
                row_count += 1
                if raw is not None:
                    payload_bytes += len(str(raw).encode("utf-8"))
                point = cls._valid_point(int(event_id), raw)
                if point is not None:
                    points.append(point)
            diag.after_query(rows=row_count, payload_bytes=payload_bytes)
            return points
        finally:
            diag.finish(note=(
                "indexed_time_window" if lower_ms is not None else "indexed_full_history_for_rebuild"
            ))

    @staticmethod
    def _point_bounds(conn: sqlite3.Connection) -> tuple[int | None, int | None, int | None]:
        row = conn.execute(
            """SELECT MIN(local_received_at_ms),MIN(source_event_id),MAX(local_received_at_ms)
               FROM outcome_deribit_point_index_v2"""
        ).fetchone()
        if row is None:
            return None, None, None
        return (
            int(row[0]) if row[0] is not None else None,
            int(row[1]) if row[1] is not None else None,
            int(row[2]) if row[2] is not None else None,
        )

    @staticmethod
    def _checkpoint(conn: sqlite3.Connection) -> tuple[int, int] | None:
        row = conn.execute(
            """SELECT MAX(outcome_snapshot_event_id), MAX(snapshot_timestamp_ms)
               FROM outcome_deribit_feature_rows WHERE feature_schema_version=?""",
            (DERIBIT_FEATURE_SCHEMA_VERSION,),
        ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None
        return int(row[0]), int(row[1])

    @staticmethod
    def _labels(current: dict[str, Any], market_times: tuple[int, ...], market_rows: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        timestamp = int(current["snapshot_timestamp_ms"])
        labels: dict[str, Any] = {}
        for horizon in DERIBIT_LABEL_HORIZONS_SEC:
            target = timestamp + horizon * 1_000
            position = bisect_left(market_times, target)
            key = f"future_{horizon}s"
            if position >= len(market_rows) or market_times[position] > target + DERIBIT_LABEL_TOLERANCE_MS:
                labels[key] = {"available": False, "reason": "future_accepted_snapshot_unavailable"}
                continue
            future = market_rows[position]
            record: dict[str, Any] = {
                "available": True, "label_timestamp_ms": int(future["snapshot_timestamp_ms"]),
                "label_lag_ms": int(future["snapshot_timestamp_ms"]) - target,
            }
            for side in ("yes", "no"):
                _bid, entry_ask, _bid_size, _ask_size = _bbo(current[f"{side}_l2"])
                future_bid, _ask, _future_bid_size, _future_ask_size = _bbo(future[f"{side}_l2"])
                record[f"{side}_future_bid"] = future_bid
                record[f"{side}_long_markout_ps"] = (
                    future_bid - entry_ask if future_bid is not None and entry_ask is not None else None
                )
            labels[key] = record
        return labels

    @staticmethod
    def _outcome_features(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        yes_bid, yes_ask, yes_bid_size, yes_ask_size = _bbo(snapshot["yes_l2"])
        no_bid, no_ask, no_bid_size, no_ask_size = _bbo(snapshot["no_l2"])
        return {
            "time_left_sec": _number(snapshot.get("time_left_sec")), "strike": _number(snapshot.get("strike")),
            "yes_bid": yes_bid, "yes_ask": yes_ask, "yes_bid_size": yes_bid_size, "yes_ask_size": yes_ask_size,
            "no_bid": no_bid, "no_ask": no_ask, "no_bid_size": no_bid_size, "no_ask_size": no_ask_size,
        }

    @staticmethod
    def _deribit_features(index: _DeribitIndex, point: _DeribitPoint, *, outcome_timestamp_ms: int) -> dict[str, Any]:
        raw = point.payload
        features = {
            "deribit_available": True,
            "deribit_age_ms": outcome_timestamp_ms - point.local_received_at_ms,
            "deribit_index_price": _number(raw.get("index_price")),
            "deribit_mid": _number(raw.get("mid")),
            "deribit_spread_bps": _number(raw.get("spread_bps")),
            "deribit_top_imbalance": _number(raw.get("top_imbalance")),
            "deribit_mark_price": _number(raw.get("mark_price")),
            "deribit_open_interest": _number(raw.get("open_interest")),
            "deribit_funding_8h": _number(raw.get("funding_8h")),
            # No trades in one second is a legitimate zero, not missing data.
            "deribit_trade_flow_imbalance_1s": _number(raw.get("trade_flow_imbalance_1s")) or 0.0,
            "deribit_trade_flow_present_1s": float(raw.get("trade_flow_imbalance_1s") is not None),
        }
        for horizon in (5, 15, 60):
            features[f"deribit_mid_return_{horizon}s_bps"] = index.return_bps(point, field="mid", horizon_sec=horizon)
            features[f"deribit_index_return_{horizon}s_bps"] = index.return_bps(point, field="index_price", horizon_sec=horizon)
        return features

    def build(
        self, *, batch_size: int = 500, rebuild: bool = False,
        progress: Callable[[int, int], None] | None = None,
        write_timeout_sec: float = 10.0,
    ) -> D2BuildResult:
        """Build D2 rows, limiting source scope to the live Deribit era."""
        self._sync_point_index(write_timeout_sec=write_timeout_sec)
        with sqlite3.connect(self.journal.db_path) as conn:
            first, first_event_id, last = self._point_bounds(conn)
            checkpoint = None if rebuild else self._checkpoint(conn)
            refresh_after = None if checkpoint is None else (
                checkpoint[1] - (max(DERIBIT_LABEL_HORIZONS_SEC) * 1_000 + DERIBIT_LABEL_TOLERANCE_MS)
            )
            snapshots = self._outcome_snapshots(
                conn, after_ms=first, after_event_id=first_event_id,
                checkpoint_event_id=checkpoint[0] if checkpoint is not None else None,
                refresh_after_ms=refresh_after,
            )
            # A valid, late source row can alter labels before the retained
            # tail.  Fall back to the existing full reconstruction behaviour.
            if checkpoint is not None and refresh_after is not None and any(
                event_id > checkpoint[0]
                and int(snapshot["snapshot_timestamp_ms"]) < refresh_after
                for event_id, snapshot in snapshots
            ):
                snapshots = self._outcome_snapshots(
                    conn, after_ms=first, after_event_id=first_event_id,
                )
                checkpoint = None
            work = snapshots
            lower_point_ms = min((int(snapshot["snapshot_timestamp_ms"]) for _, snapshot in work), default=None)
            upper_point_ms = max((int(snapshot["snapshot_timestamp_ms"]) for _, snapshot in work), default=None)
            if lower_point_ms is not None:
                lower_point_ms -= DERIBIT_MAX_JOIN_AGE_MS + max((5, 15, 60)) * 1_000
            points = self._deribit_points(
                conn, lower_ms=lower_point_ms, upper_ms=upper_point_ms,
            )
        index = _DeribitIndex(points)
        by_market: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for _event_id, snapshot in snapshots:
            by_market[int(snapshot["outcome_id"])].append(snapshot)
        market_index = {
            outcome_id: (
                tuple(int(row["snapshot_timestamp_ms"]) for row in ordered), tuple(ordered),
            ) for outcome_id, group in by_market.items()
            for ordered in (sorted(group, key=lambda row: int(row["snapshot_timestamp_ms"])),)
        }
        # The source selection already includes all newly appended rows and
        # the complete old D2 label tail.  Rebuild that selected tail so a new
        # future observation can complete labels at its lower boundary.
        joined = unavailable = stale_rejected = 0
        labels_available = {horizon: 0 for horizon in DERIBIT_LABEL_HORIZONS_SEC}

        def rows() -> Any:
            nonlocal joined, unavailable, stale_rejected
            for event_id, snapshot in work:
                timestamp = int(snapshot["snapshot_timestamp_ms"])
                candidate = index.as_of(timestamp)
                point = candidate if candidate is not None and timestamp - candidate.local_received_at_ms <= DERIBIT_MAX_JOIN_AGE_MS else None
                features = self._outcome_features(snapshot)
                context: dict[str, Any] = {
                    "market_instance": str(snapshot["outcome_id"]), "snapshot_event_id": event_id,
                    "event_time_ms": timestamp, "deribit_join_rule": "as_of_local_received_at",
                    "deribit_source": "deribit_public_ws", "live_authority": False,
                }
                if point is None:
                    unavailable += 1
                    unavailable_reason = (
                        "deribit_snapshot_stale_for_outcome_decision"
                        if candidate is not None else "no_valid_locally_received_snapshot"
                    )
                    stale_rejected += int(candidate is not None)
                    features.update({
                        "deribit_available": False, "deribit_unavailable_reason": unavailable_reason,
                        "deribit_freshness_budget_ms": DERIBIT_MAX_JOIN_AGE_MS,
                    })
                    context.update({
                        "deribit_freshness_budget_ms": DERIBIT_MAX_JOIN_AGE_MS,
                        "deribit_unavailable_reason": unavailable_reason,
                    })
                else:
                    joined += 1
                    features.update(self._deribit_features(index, point, outcome_timestamp_ms=timestamp))
                    context.update({
                        "deribit_snapshot_event_id": point.event_id,
                        "deribit_source_timestamp_ms": point.source_timestamp_ms,
                        "deribit_local_received_at_ms": point.local_received_at_ms,
                        "deribit_age_ms": timestamp - point.local_received_at_ms,
                        "deribit_freshness_budget_ms": DERIBIT_MAX_JOIN_AGE_MS,
                    })
                market_times, market_rows = market_index[int(snapshot["outcome_id"])]
                labels = self._labels(snapshot, market_times, market_rows)
                for horizon in DERIBIT_LABEL_HORIZONS_SEC:
                    labels_available[horizon] += int(labels[f"future_{horizon}s"]["available"])
                yield {
                    "feature_schema_version": DERIBIT_FEATURE_SCHEMA_VERSION,
                    "outcome_snapshot_event_id": event_id, "outcome_id": int(snapshot["outcome_id"]),
                    "period": "1d", "snapshot_timestamp_ms": timestamp,
                    "deribit_snapshot_event_id": point.event_id if point else None,
                    "deribit_source_timestamp_ms": point.source_timestamp_ms if point else None,
                    "deribit_local_received_at_ms": point.local_received_at_ms if point else None,
                    "deribit_age_ms": timestamp - point.local_received_at_ms if point else None,
                    "deribit_join_direction": "as_of_local_received_at",
                    "deribit_valid": point is not None,
                    "features": features, "labels": labels, "market_context": context,
                }

        written = self.journal.bulk_upsert_outcome_deribit_feature_rows(
            rows(), batch_size=batch_size, timeout_sec=write_timeout_sec,
            progress=(lambda completed: progress(completed, len(work))) if progress else None,
        )
        with sqlite3.connect(self.journal.db_path) as conn:
            eligible_outcome_snapshots = int(conn.execute(
                "SELECT COUNT(*) FROM outcome_deribit_feature_rows WHERE feature_schema_version=?",
                (DERIBIT_FEATURE_SCHEMA_VERSION,),
            ).fetchone()[0])
        return D2BuildResult(
            eligible_outcome_snapshots=eligible_outcome_snapshots, rows_written=written, deribit_joined=joined,
            deribit_unavailable=unavailable, deribit_stale_rejected=stale_rejected, labels_available=labels_available,
            first_deribit_received_at_ms=first,
            last_deribit_received_at_ms=last,
        )
