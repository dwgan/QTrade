from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import polars as pl
from pydantic import BaseModel, Field


class PointInTimeUniverse(BaseModel):
    as_of_date: date
    index_codes: list[str]
    index_membership_dates: dict[str, date]
    listed_security_count: int
    member_security_count: int
    final_security_count: int
    missing_historical_names: int
    warnings: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class UniverseBuildResult:
    audit: PointInTimeUniverse
    frame: pl.DataFrame


def _date_expression(column: str) -> pl.Expr:
    return (
        pl.col(column)
        .cast(pl.String)
        .str.replace_all("-", "")
        .str.strptime(pl.Date, "%Y%m%d", strict=False)
    )


class PointInTimeUniverseBuilder:
    def __init__(self, index_codes: list[str]) -> None:
        self.index_codes = list(dict.fromkeys(index_codes))
        if not self.index_codes:
            raise ValueError("Point-in-time universe requires at least one index code.")

    def build(
        self,
        as_of_date: date,
        security_master: pl.DataFrame,
        security_names: pl.DataFrame,
        index_members: pl.DataFrame,
    ) -> UniverseBuildResult:
        master = self._master(as_of_date, security_master)
        names = self._names(as_of_date, security_names)
        members, membership_dates = self._members(as_of_date, index_members)

        universe = master.join(
            members,
            left_on="ts_code",
            right_on="con_code",
            how="inner",
        )
        if not names.is_empty():
            universe = (
                universe.join(names, on="ts_code", how="left")
                .with_columns(
                    pl.coalesce("historical_name", "name").alias("name"),
                    pl.max_horizontal(
                        "security_master_available_from",
                        "membership_available_from",
                        "name_available_from",
                    ).alias("universe_available_from"),
                )
                .drop("historical_name")
            )
        else:
            universe = universe.with_columns(
                pl.max_horizontal(
                    "security_master_available_from",
                    "membership_available_from",
                ).alias("universe_available_from"),
                pl.lit(None).cast(pl.Date).alias("name_available_from"),
            )
        missing_names = universe.filter(pl.col("name_available_from").is_null()).height
        warnings = []
        if missing_names:
            warnings.append(
                f"{missing_names} universe securities have no effective-dated name record; "
                "snapshot master name was used."
            )
        if universe.is_empty():
            raise ValueError(
                f"Point-in-time universe is empty for {as_of_date}; "
                "check historical index membership and security master snapshots."
            )
        audit = PointInTimeUniverse(
            as_of_date=as_of_date,
            index_codes=self.index_codes,
            index_membership_dates=membership_dates,
            listed_security_count=master.height,
            member_security_count=members.height,
            final_security_count=universe.height,
            missing_historical_names=missing_names,
            warnings=warnings,
        )
        return UniverseBuildResult(audit=audit, frame=universe)

    @staticmethod
    def _master(as_of_date: date, frame: pl.DataFrame) -> pl.DataFrame:
        required = {"ts_code", "name", "industry", "list_date", "delist_date"}
        if missing := required - set(frame.columns):
            raise ValueError(
                f"Security master is missing point-in-time columns: "
                f"{', '.join(sorted(missing))}"
            )
        availability = (
            _date_expression("available_from")
            if "available_from" in frame.columns
            else _date_expression("list_date")
        )
        return (
            frame.with_columns(
                _date_expression("list_date"),
                _date_expression("delist_date"),
                availability.alias("security_master_available_from"),
            )
            .filter(
                (pl.col("list_date") <= as_of_date)
                & (
                    pl.col("delist_date").is_null()
                    | (pl.col("delist_date") > as_of_date)
                )
                & (pl.col("security_master_available_from") <= as_of_date)
            )
            .sort(["ts_code", "security_master_available_from"])
            .unique(subset=["ts_code"], keep="last")
        )

    @staticmethod
    def _names(as_of_date: date, frame: pl.DataFrame) -> pl.DataFrame:
        required = {"ts_code", "name", "start_date", "end_date", "ann_date"}
        if missing := required - set(frame.columns):
            raise ValueError(
                f"Security names are missing point-in-time columns: "
                f"{', '.join(sorted(missing))}"
            )
        availability = (
            _date_expression("available_from")
            if "available_from" in frame.columns
            else pl.max_horizontal(
                _date_expression("start_date"),
                _date_expression("ann_date") + pl.duration(days=1),
            )
        )
        return (
            frame.select(
                pl.col("ts_code").cast(pl.String),
                pl.col("name").cast(pl.String).alias("historical_name"),
                _date_expression("start_date"),
                _date_expression("end_date"),
                availability.alias("name_available_from"),
            )
            .filter(
                (pl.col("start_date") <= as_of_date)
                & (
                    pl.col("end_date").is_null()
                    | (pl.col("end_date") >= as_of_date)
                )
                & (pl.col("name_available_from") <= as_of_date)
            )
            .sort(["ts_code", "start_date", "name_available_from"])
            .unique(subset=["ts_code"], keep="last")
            .select("ts_code", "historical_name", "name_available_from")
        )

    def _members(
        self,
        as_of_date: date,
        frame: pl.DataFrame,
    ) -> tuple[pl.DataFrame, dict[str, date]]:
        required = {"index_code", "con_code", "trade_date"}
        if missing := required - set(frame.columns):
            raise ValueError(
                f"Index members are missing point-in-time columns: "
                f"{', '.join(sorted(missing))}"
            )
        availability = (
            _date_expression("available_from")
            if "available_from" in frame.columns
            else _date_expression("trade_date")
        )
        prepared = frame.select(
            pl.col("index_code").cast(pl.String),
            pl.col("con_code").cast(pl.String),
            _date_expression("trade_date"),
            availability.alias("membership_available_from"),
        ).filter(
            pl.col("index_code").is_in(self.index_codes)
            & (pl.col("trade_date") <= as_of_date)
            & (pl.col("membership_available_from") <= as_of_date)
        )
        selected: list[pl.DataFrame] = []
        membership_dates: dict[str, date] = {}
        for index_code in self.index_codes:
            index_frame = prepared.filter(pl.col("index_code") == index_code)
            if index_frame.is_empty():
                raise ValueError(
                    f"No historical members for {index_code} on or before {as_of_date}."
                )
            latest_date = index_frame.get_column("trade_date").max()
            membership_dates[index_code] = latest_date
            selected.append(index_frame.filter(pl.col("trade_date") == latest_date))
        return (
            pl.concat(selected, how="vertical_relaxed")
            .group_by("con_code")
            .agg(
                pl.col("membership_available_from")
                .max()
                .alias("membership_available_from")
            ),
            membership_dates,
        )
