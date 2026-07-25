from datetime import date, timedelta

import polars as pl

from qtrade.config import IndustryConfig
from qtrade.industry.analyzer import IndustryAnalyzer

AS_OF_DATE = date(2026, 7, 24)


def trading_dates(count: int) -> list[date]:
    current = AS_OF_DATE
    values: list[date] = []
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current)
        current -= timedelta(days=1)
    return list(reversed(values))


def make_index_history() -> pl.DataFrame:
    rows = []
    slopes = {"000300.SH": 1.0, "000905.SH": 2.0, "000852.SH": 0.2}
    for code, slope in slopes.items():
        for offset, trading_date in enumerate(trading_dates(80)):
            rows.append(
                {
                    "ts_code": code,
                    "trade_date": trading_date.strftime("%Y%m%d"),
                    "close": 3000 + offset * slope,
                }
            )
    return pl.DataFrame(rows)


def make_stock_history() -> tuple[pl.DataFrame, pl.DataFrame]:
    rows = []
    master = []
    industries = {"科技": 0.08, "消费": 0.03, "公用事业": -0.01}
    dates = trading_dates(80)
    code_number = 1
    for industry, slope in industries.items():
        for member in range(6):
            code = f"{code_number:06d}.SZ"
            code_number += 1
            master.append({"ts_code": code, "industry": industry})
            previous = None
            for offset, trading_date in enumerate(dates):
                close = 10 + member * 0.1 + offset * slope
                rows.append(
                    {
                        "ts_code": code,
                        "trade_date": trading_date.strftime("%Y%m%d"),
                        "close": close,
                        "pre_close": close if previous is None else previous,
                        "amount": 1000 + offset * (20 if industry == "科技" else 5),
                    }
                )
                previous = close
    return pl.DataFrame(rows), pl.DataFrame(master)


def test_industry_ranking_and_style_direction() -> None:
    stocks, master = make_stock_history()
    result = IndustryAnalyzer(IndustryConfig(minimum_stocks=5), "000300.SH").analyze(
        AS_OF_DATE,
        stocks,
        make_index_history(),
        master,
        AS_OF_DATE,
    )

    assert len(result.industries) == 3
    assert result.industries[0].name == "科技"
    assert result.industries[0].rank == 1
    assert result.industries[0].above_ma_60 == 1
    styles = {style.name: style for style in result.styles}
    assert styles["mid_vs_large"].leader == "numerator"
    assert styles["small_vs_large"].leader == "denominator"
    assert result.data_confidence == "high"


def test_stale_classification_snapshot_reduces_confidence() -> None:
    stocks, master = make_stock_history()
    result = IndustryAnalyzer(IndustryConfig(minimum_stocks=5), "000300.SH").analyze(
        AS_OF_DATE,
        stocks,
        make_index_history(),
        master,
        AS_OF_DATE - timedelta(days=1),
    )

    assert result.data_confidence == "medium"
    assert any("行业分类快照日期" in warning for warning in result.warnings)
