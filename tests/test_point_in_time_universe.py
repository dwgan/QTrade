from datetime import date

import polars as pl
import pytest

from qtrade.factors.universe import PointInTimeUniverseBuilder

AS_OF_DATE = date(2022, 6, 30)


def universe_inputs() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    master = pl.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"],
            "name": ["Current A", "Current B", "Future", "Delisted"],
            "industry": ["Tech", "Consumer", "Tech", "Industry"],
            "list_date": ["20100101", "20120101", "20230101", "20100101"],
            "delist_date": [None, None, None, "20211231"],
            "available_from": ["20220601"] * 4,
        }
    )
    names = pl.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ"],
            "name": ["*ST Historical A", "Future Name B"],
            "start_date": ["20220101", "20220101"],
            "end_date": [None, None],
            "ann_date": ["20211231", "20220701"],
            "available_from": ["20220101", "20220702"],
        }
    )
    members = pl.DataFrame(
        {
            "index_code": [
                "000300.SH",
                "000300.SH",
                "000905.SH",
                "000905.SH",
            ],
            "con_code": [
                "000004.SZ",
                "000001.SZ",
                "000004.SZ",
                "000002.SZ",
            ],
            "trade_date": ["20220301", "20220601", "20220301", "20220615"],
            "available_from": ["20220301", "20220601", "20220301", "20220615"],
        }
    )
    return master, names, members


def test_point_in_time_universe_uses_listing_status_names_and_latest_members() -> None:
    result = PointInTimeUniverseBuilder(["000300.SH", "000905.SH"]).build(
        AS_OF_DATE,
        *universe_inputs(),
    )

    assert result.frame.get_column("ts_code").sort().to_list() == [
        "000001.SZ",
        "000002.SZ",
    ]
    first = result.frame.filter(pl.col("ts_code") == "000001.SZ").row(
        0, named=True
    )
    second = result.frame.filter(pl.col("ts_code") == "000002.SZ").row(
        0, named=True
    )
    assert first["name"] == "*ST Historical A"
    assert second["name"] == "Current B"
    assert first["universe_available_from"] == date(2022, 6, 1)
    assert result.audit.index_membership_dates == {
        "000300.SH": date(2022, 6, 1),
        "000905.SH": date(2022, 6, 15),
    }
    assert result.audit.listed_security_count == 2
    assert result.audit.final_security_count == 2
    assert result.audit.missing_historical_names == 1


def test_point_in_time_universe_requires_each_configured_index() -> None:
    master, names, members = universe_inputs()

    with pytest.raises(ValueError, match="000852.SH"):
        PointInTimeUniverseBuilder(["000300.SH", "000852.SH"]).build(
            AS_OF_DATE,
            master,
            names,
            members,
        )
