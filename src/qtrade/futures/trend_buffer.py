from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class FuturesPositionBufferPolicy:
    relative_threshold: float = 0.10
    minimum_lots: int = 1

    def __post_init__(self) -> None:
        if not math.isfinite(self.relative_threshold) or not 0 <= self.relative_threshold <= 1:
            raise ValueError("relative_threshold must be finite and in [0, 1].")
        if isinstance(self.minimum_lots, bool) or not isinstance(self.minimum_lots, int):
            raise ValueError("minimum_lots must be an integer.")
        if self.minimum_lots < 0:
            raise ValueError("minimum_lots must be non-negative.")


@dataclass(frozen=True)
class FuturesPositionBufferResult:
    signed_lots: int
    applied: bool
    reason: str | None


class FuturesPositionBuffer:
    def __init__(self, policy: FuturesPositionBufferPolicy) -> None:
        self.policy = policy

    def apply(
        self,
        previous_signed_lots: int,
        desired_signed_lots: int,
    ) -> FuturesPositionBufferResult:
        self._lots(previous_signed_lots, "previous_signed_lots")
        self._lots(desired_signed_lots, "desired_signed_lots")
        previous_direction = self._sign(previous_signed_lots)
        desired_direction = self._sign(desired_signed_lots)
        if (
            previous_direction == 0
            or desired_direction == 0
            or previous_direction != desired_direction
        ):
            return FuturesPositionBufferResult(desired_signed_lots, False, None)
        threshold = max(
            self.policy.minimum_lots,
            math.floor(abs(previous_signed_lots) * self.policy.relative_threshold),
        )
        if abs(desired_signed_lots - previous_signed_lots) <= threshold:
            return FuturesPositionBufferResult(
                previous_signed_lots,
                True,
                "within_position_buffer",
            )
        return FuturesPositionBufferResult(desired_signed_lots, False, None)

    @staticmethod
    def _lots(value: int, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer.")

    @staticmethod
    def _sign(value: int) -> int:
        return (value > 0) - (value < 0)
