from __future__ import annotations

from qtrade.futures.trend_buffer import FuturesPositionBuffer, FuturesPositionBufferPolicy


def test_small_same_direction_change_holds_previous_whole_lots() -> None:
    result = FuturesPositionBuffer(FuturesPositionBufferPolicy()).apply(
        previous_signed_lots=20,
        desired_signed_lots=22,
    )

    assert result.signed_lots == 20
    assert result.applied
    assert result.reason == "within_position_buffer"


def test_change_beyond_buffer_uses_new_target() -> None:
    result = FuturesPositionBuffer(FuturesPositionBufferPolicy()).apply(20, 23)

    assert result.signed_lots == 23
    assert not result.applied
    assert result.reason is None


def test_open_close_and_reversal_are_never_buffered() -> None:
    buffer = FuturesPositionBuffer(FuturesPositionBufferPolicy())

    assert buffer.apply(0, 1).signed_lots == 1
    assert buffer.apply(10, 0).signed_lots == 0
    assert buffer.apply(10, -1).signed_lots == -1
    assert not buffer.apply(0, 1).applied
    assert not buffer.apply(10, 0).applied
    assert not buffer.apply(10, -1).applied
