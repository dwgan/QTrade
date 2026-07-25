from datetime import date, timedelta

import polars as pl

from qtrade.config import MarketConfig
from qtrade.market.analyzer import MarketAnalyzer
from qtrade.market.models import MarketState

AS_OF_DATE = date(2026, 7, 24)
INDEX_CODES = ["000300.SH", "000905.SH", "000852.SH"]


def trading_dates(count: int) -> list[date]:
    current = AS_OF_DATE
    values: list[date] = []
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current)
        current -= timedelta(days=1)
    return list(reversed(values))


def index_history(count: int, rising: bool = True) -> pl.DataFrame:
    rows = []
    dates = trading_dates(count)
    for code_offset, code in enumerate(INDEX_CODES):
        for offset, trading_date in enumerate(dates):
            direction = offset if rising else count - offset
            rows.append(
                {
                    "ts_code": code,
                    "trade_date": trading_date.strftime("%Y%m%d"),
                    "close": 3000 + code_offset * 100 + direction * 3,
                }
            )
    return pl.DataFrame(rows)


def stock_history(count: int, stock_count: int, rising: bool = True) -> pl.DataFrame:
    rows = []
    dates = trading_dates(count)
    for stock in range(stock_count):
        previous = None
        for offset, trading_date in enumerate(dates):
            direction = offset if rising else count - offset
            close = 10 + stock * 0.01 + direction * 0.02
            rows.append(
                {
                    "ts_code": f"{stock:06d}.SZ",
                    "trade_date": trading_date.strftime("%Y%m%d"),
                    "close": close,
                    "pre_close": close - 0.02 if previous is None else previous,
                }
            )
            previous = close
    return pl.DataFrame(rows)


def config() -> MarketConfig:
    return MarketConfig(
        index_codes=INDEX_CODES,
        primary_index_code="000300.SH",
        minimum_history_days=120,
        minimum_breadth_stocks=100,
    )


def test_rising_market_is_classified_as_attack() -> None:
    result = MarketAnalyzer(config()).analyze(
        AS_OF_DATE,
        index_history(220, rising=True),
        stock_history(220, 120, rising=True),
    )

    assert result.state == MarketState.ATTACK
    assert result.temperature is not None and result.temperature >= 70
    assert result.trend_score == 100
    assert result.breadth.above_ma_120 == 1
    assert result.breadth.advance_ratio == 1
    assert result.data_confidence == "high"
    assert result.warnings == []


def test_falling_market_is_classified_as_high_risk() -> None:
    result = MarketAnalyzer(config()).analyze(
        AS_OF_DATE,
        index_history(220, rising=False),
        stock_history(220, 120, rising=False),
    )

    assert result.state in {MarketState.DEFENSIVE, MarketState.HIGH_RISK}
    assert result.temperature is not None and result.temperature < 50
    assert result.trend_score == 0
    assert result.breadth.above_ma_60 == 0
    assert result.breadth.advance_ratio == 0


def test_short_history_does_not_emit_temperature() -> None:
    result = MarketAnalyzer(config()).analyze(
        AS_OF_DATE,
        index_history(30, rising=True),
        stock_history(30, 120, rising=True),
    )

    assert result.state == MarketState.INSUFFICIENT_DATA
    assert result.temperature is None
    assert result.data_confidence == "insufficient"
    assert any("少于要求" in warning for warning in result.warnings)
