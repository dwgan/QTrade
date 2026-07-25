from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import polars as pl

from qtrade.config import MarketConfig, ProviderConfig
from qtrade.domain import DataBatch, Dataset, FetchRequest


class TushareProvider:
    """Tushare Pro adapter.

    Provider-specific field names are retained in the raw batch. Curated
    normalization is handled by the ingestion service.
    """

    def __init__(
        self,
        config: ProviderConfig,
        market: MarketConfig,
        client: Any | None = None,
    ) -> None:
        self._config = config
        self._market = market
        self._client = client or self._create_client()

    @property
    def name(self) -> str:
        return "tushare"

    def _create_client(self) -> Any:
        try:
            import tushare as ts
        except ImportError as exc:
            raise RuntimeError(
                "Tushare is not installed. Install the project dependencies first."
            ) from exc
        return ts.pro_api(self._config.token())

    @staticmethod
    def _date(value: Any) -> str:
        return value.strftime("%Y%m%d")

    @staticmethod
    def _to_polars(value: Any) -> pl.DataFrame:
        if value is None:
            return pl.DataFrame()
        if isinstance(value, pl.DataFrame):
            return value
        if hasattr(value, "to_dict"):
            return pl.DataFrame(value.to_dict(orient="list"))
        return pl.DataFrame(value)

    def _call(self, operation: Callable[..., Any], **kwargs: Any) -> pl.DataFrame:
        last_error: Exception | None = None
        for attempt in range(1, self._config.retry_attempts + 1):
            try:
                result = operation(**kwargs)
                if self._config.request_pause_seconds:
                    time.sleep(self._config.request_pause_seconds)
                return self._to_polars(result)
            except Exception as exc:  # Provider SDK exceptions are not stable.
                last_error = exc
                if attempt < self._config.retry_attempts:
                    time.sleep(self._config.request_pause_seconds * attempt)
        assert last_error is not None
        raise RuntimeError(
            f"Tushare request failed after {self._config.retry_attempts} attempts: {last_error}"
        ) from last_error

    def fetch(self, dataset: Dataset, request: FetchRequest) -> DataBatch:
        fetcher = {
            Dataset.TRADE_CALENDAR: self._fetch_trade_calendar,
            Dataset.SECURITY_MASTER: self._fetch_security_master,
            Dataset.SECURITY_NAMES: self._fetch_security_names,
            Dataset.DAILY_PRICES: self._fetch_daily_prices,
            Dataset.ADJUST_FACTORS: self._fetch_adjust_factors,
            Dataset.INDEX_DAILY: self._fetch_index_daily,
            Dataset.INDEX_MEMBERS: self._fetch_index_members,
        }[dataset]
        frame, params = fetcher(request)
        return DataBatch(
            dataset=dataset,
            provider=self.name,
            as_of_date=request.as_of_date,
            frame=frame,
            request=params,
        )

    def _fetch_trade_calendar(self, request: FetchRequest) -> tuple[pl.DataFrame, dict[str, Any]]:
        params = {
            "exchange": self._market.exchange,
            "start_date": self._date(request.start_date or request.as_of_date),
            "end_date": self._date(request.end_date or request.as_of_date),
        }
        return self._call(self._client.trade_cal, **params), params

    def _fetch_security_master(self, request: FetchRequest) -> tuple[pl.DataFrame, dict[str, Any]]:
        fields = (
            "ts_code,symbol,name,area,industry,fullname,enname,cnspell,market,"
            "exchange,curr_type,list_status,list_date,delist_date,is_hs,act_name,act_ent_type"
        )
        frames: list[pl.DataFrame] = []
        for status in ("L", "D", "P"):
            frames.append(
                self._call(
                    self._client.stock_basic,
                    exchange="",
                    list_status=status,
                    fields=fields,
                )
            )
        frame = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        return frame, {"exchange": "", "list_status": ["L", "D", "P"], "fields": fields}

    def _fetch_security_names(self, request: FetchRequest) -> tuple[pl.DataFrame, dict[str, Any]]:
        params = {"fields": "ts_code,name,start_date,end_date,ann_date,change_reason"}
        return self._call(self._client.namechange, **params), params

    def _fetch_daily_prices(self, request: FetchRequest) -> tuple[pl.DataFrame, dict[str, Any]]:
        params = {"trade_date": self._date(request.as_of_date)}
        return self._call(self._client.daily, **params), params

    def _fetch_adjust_factors(self, request: FetchRequest) -> tuple[pl.DataFrame, dict[str, Any]]:
        params = {"trade_date": self._date(request.as_of_date)}
        return self._call(self._client.adj_factor, **params), params

    def _fetch_index_daily(self, request: FetchRequest) -> tuple[pl.DataFrame, dict[str, Any]]:
        trade_date = self._date(request.as_of_date)
        frames = [
            self._call(self._client.index_daily, ts_code=code, trade_date=trade_date)
            for code in self._market.index_codes
        ]
        frame = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        return frame, {"ts_code": self._market.index_codes, "trade_date": trade_date}

    def _fetch_index_members(self, request: FetchRequest) -> tuple[pl.DataFrame, dict[str, Any]]:
        start = request.start_date or request.as_of_date - timedelta(days=45)
        end = request.end_date or request.as_of_date
        params = {
            "index_code": self._market.index_codes,
            "start_date": self._date(start),
            "end_date": self._date(end),
        }
        frames = [
            self._call(
                self._client.index_weight,
                index_code=code,
                start_date=params["start_date"],
                end_date=params["end_date"],
            )
            for code in self._market.index_codes
        ]
        frame = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        return frame, params
