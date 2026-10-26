"""Hard execution limits and budget accounting.

The ledger is the single place that decides whether another model call is
allowed. It reserves before a call and settles the real cost afterwards, so
parallel branches cannot collectively overshoot by more than their combined
reservations.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


class LimitExceeded(RuntimeError):
    """Base class for a refused call."""

    status = "limit_exceeded"


class BudgetExceeded(LimitExceeded):
    status = "budget_exceeded"


class CallLimitExceeded(LimitExceeded):
    status = "limit_exceeded"


class DeadlineExceeded(LimitExceeded):
    status = "limit_exceeded"


@dataclass
class BudgetLedger:
    max_usd: float
    max_calls: int
    max_wall_seconds: int
    reservation_usd: float
    monetary_enforced: bool = False
    started_monotonic: float = field(default_factory=time.monotonic)
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    calls: int = 0
    denied: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def deadline_monotonic(self) -> float:
        return self.started_monotonic + self.max_wall_seconds

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.max_usd - self.spent_usd - self.reserved_usd)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic

    async def reserve(self, estimate_usd: float) -> float:
        """Reserve budget for one call and return the reserved amount."""
        async with self._lock:
            if time.monotonic() >= self.deadline_monotonic:
                self.denied += 1
                raise DeadlineExceeded(
                    f"wall-clock limit of {self.max_wall_seconds}s exceeded"
                )
            if self.calls >= self.max_calls:
                self.denied += 1
                raise CallLimitExceeded(f"agent call limit of {self.max_calls} reached")
            estimate = max(0.0, estimate_usd)
            if self.spent_usd + self.reserved_usd + estimate > self.max_usd + 1e-9:
                self.denied += 1
                raise BudgetExceeded(
                    f"budget of ${self.max_usd:.4f} would be exceeded "
                    f"(spent ${self.spent_usd:.4f}, reserved ${self.reserved_usd:.4f})"
                )
            self.reserved_usd += estimate
            self.calls += 1
            return estimate

    async def settle(self, reserved_usd: float, actual_usd: float) -> None:
        async with self._lock:
            self.reserved_usd = max(0.0, self.reserved_usd - reserved_usd)
            self.spent_usd += max(0.0, actual_usd)

    async def release(self, reserved_usd: float) -> None:
        """Return a reservation when the call never produced usage."""
        async with self._lock:
            self.reserved_usd = max(0.0, self.reserved_usd - reserved_usd)

    def snapshot(self) -> dict[str, float | int]:
        return {
            "max_usd": round(self.max_usd, 6),
            "spent_usd": round(self.spent_usd, 6),
            "reserved_usd": round(self.reserved_usd, 6),
            "remaining_usd": round(self.remaining_usd, 6),
            "max_calls": self.max_calls,
            "calls": self.calls,
            "denied": self.denied,
            "monetary_enforced": self.monetary_enforced,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "remaining_seconds": round(
                max(0.0, self.deadline_monotonic - time.monotonic()), 3
            ),
        }


def estimate_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    input_rate_per_million: float,
    output_rate_per_million: float,
) -> float:
    return (input_tokens * input_rate_per_million + output_tokens * output_rate_per_million) / 1_000_000