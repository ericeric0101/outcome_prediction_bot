"""Shared wall-clock budget for one synchronous Outcome execution tick."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import time
from typing import Any, Callable, Iterator, TypeVar


class OutcomeTickBudgetExceeded(TimeoutError):
    """The current execution tick exhausted its total I/O budget."""


_deadline: ContextVar[float | None] = ContextVar("outcome_tick_deadline", default=None)
_F = TypeVar("_F", bound=Callable[..., Any])


def remaining_outcome_tick_budget_sec() -> float | None:
    deadline = _deadline.get()
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def require_outcome_tick_budget(operation: str) -> float | None:
    remaining = remaining_outcome_tick_budget_sec()
    if remaining is not None and remaining <= 0:
        raise OutcomeTickBudgetExceeded(
            f"Outcome execution tick budget exhausted before {operation}; current tick aborted fail-closed"
        )
    return remaining


@contextmanager
def outcome_tick_budget(seconds: float) -> Iterator[None]:
    """Install a deadline, preserving an earlier enclosing tick deadline."""
    proposed = time.monotonic() + max(0.001, float(seconds))
    enclosing = _deadline.get()
    deadline = min(proposed, enclosing) if enclosing is not None else proposed
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def bounded_outcome_tick(method: _F) -> _F:
    """Bound a runtime entrypoint to 12 seconds including nested reads/SDK IO."""
    @wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        with outcome_tick_budget(12.0):
            return method(self, *args, **kwargs)
    return wrapped  # type: ignore[return-value]
