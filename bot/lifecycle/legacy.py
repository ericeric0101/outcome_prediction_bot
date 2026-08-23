from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


def select_market_outcome_instruments(
    btc_instruments: list[dict[str, Any]],
    current_market_slug: str,
    extract_outcome: Callable[[Any], str],
    fallback_instrument_id: Any,
) -> tuple[Any, Any, bool, bool]:
    up_instrument = None
    down_instrument = None
    for item in btc_instruments:
        if str(item.get("slug") or "") != current_market_slug:
            continue
        instrument = item.get("instrument")
        if instrument is None:
            continue
        instrument_id = getattr(instrument, "id", None)
        if instrument_id is None:
            continue
        outcome = extract_outcome(instrument)
        if outcome == "up" and up_instrument is None:
            up_instrument = instrument_id
        elif outcome == "down" and down_instrument is None:
            down_instrument = instrument_id
    matched_up = up_instrument is not None
    matched_down = down_instrument is not None
    if up_instrument is None:
        up_instrument = fallback_instrument_id
    return up_instrument, down_instrument, matched_up, matched_down


def filter_alive_market_candidates(
    btc_instruments: list[dict[str, Any]],
    current_timestamp: int,
) -> list[dict[str, Any]]:
    alive: list[dict[str, Any]] = []
    for item in btc_instruments:
        end_ts = item.get("end_timestamp")
        closed = bool(item.get("closed", False))
        if closed:
            continue
        if end_ts is not None and end_ts < (current_timestamp - 60):
            continue
        alive.append(item)
    return alive or btc_instruments


def select_current_or_next_market(
    btc_instruments: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    current_markets = [item for item in btc_instruments if item["time_diff_minutes"] <= 0 and item["time_diff_minutes"] > -15]
    future_markets = [item for item in btc_instruments if item["time_diff_minutes"] > 0]
    if current_markets:
        current_markets.sort(key=lambda item: abs(item["time_diff_minutes"]))
        return current_markets[0], None
    if future_markets:
        future_markets.sort(key=lambda item: item["time_diff_minutes"])
        return future_markets[0], "future"
    return None, "none"


@dataclass
class MarketSelection:
    selected_market: dict[str, Any]
    current_market_slug: str
    current_market_end_timestamp: int | None
    current_market_instruments: list[Any]
    instrument_id: Any
    up_instrument_id: Any
    down_instrument_id: Any
    matched_up: bool
    matched_down: bool


@dataclass
class PhaseDecision:
    next_phase_value: str
    set_settling_since: bool = False


@dataclass
class LifecycleTimerAction:
    action: str
    wait_sec: float | None = None
    should_reload_instrument: bool = False
    should_search_next: bool = False


def collect_btc_market_candidates(instruments: list[Any], startup_verbose: bool = False) -> tuple[list[dict[str, Any]], int]:
    now = datetime.now(timezone.utc)
    current_timestamp = int(now.timestamp())
    btc_instruments: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for instrument in instruments:
        try:
            inst_id = str(getattr(instrument, "id", ""))
            if inst_id in seen_ids:
                continue
            seen_ids.add(inst_id)
            if not hasattr(instrument, "info") or not instrument.info:
                continue
            question = instrument.info.get("question", "").lower()
            slug = instrument.info.get("market_slug", "").lower()
            if ("btc" not in question and "btc" not in slug) or "15m" not in slug:
                continue
            try:
                market_timestamp = int(slug.split("-")[-1])
            except (ValueError, IndexError):
                continue
            end_date = instrument.info.get("end_date_iso")
            end_timestamp = None
            if end_date:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                end_timestamp = int(end_dt.timestamp())
            time_diff = market_timestamp - current_timestamp
            btc_instruments.append(
                {
                    "instrument": instrument,
                    "slug": slug,
                    "market_timestamp": market_timestamp,
                    "end_timestamp": end_timestamp,
                    "question": question,
                    "active": instrument.info.get("active", False),
                    "closed": instrument.info.get("closed", True),
                    "time_diff_minutes": time_diff / 60,
                }
            )
        except Exception:
            continue
    return btc_instruments, current_timestamp


def resolve_bi_side_market_selection(
    btc_instruments: list[dict[str, Any]],
    current_timestamp: int,
    extract_outcome: Callable[[Any], str],
    preferred_slug: str | None = None,
) -> tuple[MarketSelection | None, str | None, int, int]:
    filtered = filter_alive_market_candidates(
        btc_instruments=btc_instruments,
        current_timestamp=current_timestamp,
    )
    current_markets = [item for item in filtered if item["time_diff_minutes"] <= 0 and item["time_diff_minutes"] > -15]
    future_markets = [item for item in filtered if item["time_diff_minutes"] > 0]
    selected = None
    selection_kind = None
    preferred_slug_norm = str(preferred_slug or "").strip().lower()
    if preferred_slug_norm:
        for item in filtered:
            if str(item.get("slug") or "") == preferred_slug_norm:
                selected = item
                break
        if selected is None:
            for item in btc_instruments:
                if str(item.get("slug") or "") != preferred_slug_norm:
                    continue
                start_ts = item.get("market_timestamp")
                end_ts = item.get("end_timestamp") or ((start_ts + 900) if start_ts else None)
                if end_ts is not None and end_ts < (current_timestamp - 60):
                    continue
                selected = item
                selection_kind = "preferred"
                break
    if selected is None:
        selected, selection_kind = select_current_or_next_market(filtered)
    if selected is None:
        return None, selection_kind, len(current_markets), len(future_markets)

    current_market_slug = str(selected.get("slug") or "")
    start_ts = selected.get("market_timestamp")
    current_market_end_timestamp = (start_ts + 900) if start_ts else None
    up_instrument, down_instrument, matched_up, matched_down = select_market_outcome_instruments(
        btc_instruments=filtered,
        current_market_slug=current_market_slug,
        extract_outcome=extract_outcome,
        fallback_instrument_id=selected["instrument"].id,
    )
    chosen_instrument = up_instrument or down_instrument or selected["instrument"].id
    market_instruments = [inst for inst in [up_instrument, down_instrument] if inst is not None]
    selection = MarketSelection(
        selected_market=selected,
        current_market_slug=current_market_slug,
        current_market_end_timestamp=current_market_end_timestamp,
        current_market_instruments=market_instruments or [chosen_instrument],
        instrument_id=chosen_instrument,
        up_instrument_id=up_instrument,
        down_instrument_id=down_instrument,
        matched_up=matched_up,
        matched_down=matched_down,
    )
    return selection, selection_kind, len(current_markets), len(future_markets)


def evaluate_market_phase(
    current_phase_value: str,
    end_ts: float | None,
    now_ts: float,
    min_minutes_to_close: float,
    settling_since_ts: float,
    settling_grace_sec: float,
) -> PhaseDecision | None:
    if end_ts is None:
        if current_phase_value not in ("WAITING", "SETTLING"):
            return PhaseDecision(next_phase_value="WAITING")
        return None

    time_left_sec = end_ts - now_ts
    if time_left_sec > min_minutes_to_close * 60:
        if current_phase_value != "ACTIVE":
            return PhaseDecision(next_phase_value="ACTIVE")
        return None

    if time_left_sec > 0:
        if current_phase_value not in ("REDUCE_ONLY", "SETTLING"):
            return PhaseDecision(next_phase_value="REDUCE_ONLY")
        return None

    if current_phase_value == "SETTLING":
        if now_ts - settling_since_ts >= settling_grace_sec:
            return PhaseDecision(next_phase_value="WAITING")
        return None

    if current_phase_value != "WAITING":
        return PhaseDecision(next_phase_value="SETTLING", set_settling_since=True)
    return None


def select_next_market_window(
    btc_slugs: list[str],
    now_ts: float,
) -> tuple[str | None, int | None]:
    best_slug = None
    best_start_ts = None
    for slug in btc_slugs:
        try:
            start_ts = int(slug.rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            continue
        end_ts = start_ts + 900
        if end_ts <= now_ts:
            continue
        if best_start_ts is None or start_ts < best_start_ts:
            best_slug = slug
            best_start_ts = start_ts
    return best_slug, best_start_ts


def determine_lifecycle_timer_action(
    phase_value: str,
    now_ts: float,
    end_ts: float | None,
    min_minutes_to_close: float,
    settling_grace_sec: float,
    market_settling_since_ts: float,
) -> LifecycleTimerAction:
    if phase_value == "ACTIVE":
        if end_ts is None:
            return LifecycleTimerAction(action="active_reload", wait_sec=60.0, should_reload_instrument=True)
        time_until_reduce = end_ts - now_ts - (min_minutes_to_close * 60)
        if time_until_reduce > 30:
            sleep_sec = min(time_until_reduce - 10, 60)
            return LifecycleTimerAction(action="active_wait", wait_sec=max(5.0, sleep_sec))
        return LifecycleTimerAction(action="active_wait", wait_sec=5.0)
    if phase_value == "REDUCE_ONLY":
        return LifecycleTimerAction(action="reduce_only_wait", wait_sec=5.0)
    if phase_value == "SETTLING":
        remaining_grace = settling_grace_sec - (now_ts - market_settling_since_ts)
        if remaining_grace > 0:
            return LifecycleTimerAction(action="settling_wait", wait_sec=min(remaining_grace, 5.0))
        return LifecycleTimerAction(action="settling_wait", wait_sec=0.0)
    return LifecycleTimerAction(action="waiting_search", should_search_next=True)
