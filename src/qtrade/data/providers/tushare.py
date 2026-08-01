from __future__ import annotations

import csv
import io
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import polars as pl
import requests

from qtrade.config import MarketConfig, ProviderConfig
from qtrade.domain import DataBatch, Dataset, FetchRequest


class TushareProvider:
    """Tushare Pro adapter.

    Provider-specific field names are retained in the raw batch. Curated
    normalization is handled by the ingestion service.
    """

    FUTURES_OPERATIONS = frozenset(
        {
            "fut_basic",
            "fut_daily",
            "fut_mapping",
            "fut_settle",
            "ft_limit",
            "trade_cal",
        }
    )

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
                "Tushare or one of its runtime dependencies is unavailable. "
                "Install the project dependencies first."
            ) from exc
        client = ts.pro_api(self._config.token())
        if api_url := self._config.api_url():
            client._DataApi__http_url = api_url
        return client

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
            if hasattr(value, "notna") and hasattr(value, "astype"):
                value = value.astype(object).where(value.notna(), None)
            return pl.DataFrame(
                value.to_dict(orient="list"),
                infer_schema_length=None,
                strict=False,
            )
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
            Dataset.INDUSTRY_MEMBERS: self._fetch_industry_members,
            Dataset.DAILY_BASIC: self._fetch_daily_basic,
            Dataset.STOCK_LIMIT: self._fetch_stock_limit,
            Dataset.FINANCIAL_INDICATORS: self._fetch_financial_indicators,
        }[dataset]
        frame, params = fetcher(request)
        return DataBatch(
            dataset=dataset,
            provider=self.name,
            as_of_date=request.as_of_date,
            frame=frame,
            request=params,
        )

    def query(self, operation: str, **params: Any) -> pl.DataFrame:
        if operation not in self.FUTURES_OPERATIONS:
            raise ValueError(f"Unsupported Tushare query operation: {operation}")
        if operation == "ft_limit" and self._config.mcp_url():
            return self._query_mcp(operation, **params)
        return self._call(getattr(self._client, operation), **params)

    def _query_mcp(self, operation: str, **params: Any) -> pl.DataFrame:
        url = self._config.mcp_url()
        if not url:
            raise RuntimeError("Tushare MCP URL is not configured.")
        page_size = int(params.pop("mcp_page_size", 50))
        if page_size < 1 or page_size > 50:
            raise ValueError("Tushare MCP page size must be between 1 and 50.")
        fields = str(params.pop("fields", ""))
        first_page, total = self._query_mcp_page(
            url,
            operation,
            params,
            fields,
            page_size,
            0,
        )
        rows = list(first_page)
        if total is not None:
            offsets = list(range(len(first_page), total, page_size))

            def fetch(offset: int) -> tuple[list[dict[str, str]], int | None]:
                return self._query_mcp_page(
                    url,
                    operation,
                    params,
                    fields,
                    page_size,
                    offset,
                )

            workers = min(self._config.mcp_parallel_requests, len(offsets))
            if workers:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    for offset, (page, reported_total) in zip(
                        offsets,
                        executor.map(fetch, offsets),
                        strict=True,
                    ):
                        if reported_total is not None and reported_total != total:
                            raise RuntimeError(
                                "Tushare MCP reported inconsistent pagination totals."
                            )
                        expected = min(page_size, total - offset)
                        if len(page) != expected:
                            raise RuntimeError(
                                f"Tushare MCP page at offset {offset} returned "
                                f"{len(page)} rows; expected {expected}."
                            )
                        rows.extend(page)
        else:
            offset = len(first_page)
            page = first_page
            while len(page) == page_size:
                page, reported_total = self._query_mcp_page(
                    url,
                    operation,
                    params,
                    fields,
                    page_size,
                    offset,
                )
                if reported_total is not None:
                    total = reported_total
                rows.extend(page)
                offset += len(page)
        if total is not None and len(rows) != total:
            raise RuntimeError(
                f"Tushare MCP returned {len(rows)} rows; expected total {total}."
            )
        keys = [(row.get("trade_date"), row.get("ts_code")) for row in rows]
        if len(keys) != len(set(keys)):
            raise RuntimeError("Tushare MCP returned duplicate futures limit rows.")
        frame = pl.DataFrame(rows, infer_schema_length=None, strict=False)
        numeric_columns = [
            name for name in ("up_limit", "down_limit", "m_ratio") if name in frame.columns
        ]
        return frame.with_columns(
            *(
                pl.col(name).cast(pl.Float64, strict=False).alias(name)
                for name in numeric_columns
            ),
            pl.lit("mcp_query_data").alias("source_transport"),
            pl.lit(total).cast(pl.Int64).alias("source_reported_total"),
        )

    def _query_mcp_page(
        self,
        url: str,
        operation: str,
        params: dict[str, Any],
        fields: str,
        page_size: int,
        offset: int,
    ) -> tuple[list[dict[str, str]], int | None]:
            page_params = {**params, "limit": page_size, "offset": offset}
            arguments: dict[str, Any] = {
                "api_name": operation,
                "params": page_params,
            }
            if fields:
                arguments["fields"] = fields
            payload = {
                "jsonrpc": "2.0",
                "id": offset + 1,
                "method": "tools/call",
                "params": {"name": "query_data", "arguments": arguments},
            }
            body = self._post_mcp(url, payload)
            if error := body.get("error"):
                raise RuntimeError(f"Tushare MCP request failed: {error}")
            result = body.get("result") or {}
            content = result.get("content") or []
            text = next(
                (item.get("text", "") for item in content if item.get("type") == "text"),
                "",
            )
            if result.get("isError"):
                raise RuntimeError(f"Tushare MCP request failed: {text}")
            match = re.search(r"共\s*(\d+)\s*条", text)
            total = int(match.group(1)) if match else None
            csv_text = "\n".join(
                line for line in text.splitlines() if not line.startswith("...")
            )
            page = list(csv.DictReader(io.StringIO(csv_text))) if csv_text.strip() else []
            if self._config.mcp_request_pause_seconds:
                time.sleep(self._config.mcp_request_pause_seconds)
            return page, total

    def _post_mcp(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self._config.retry_attempts + 1):
            try:
                response = requests.post(
                    url,
                    json=payload,
                    headers={"Accept": "application/json, text/event-stream"},
                    timeout=60,
                )
                response.raise_for_status()
                body = response.json()
                result = body.get("result") or {}
                content = result.get("content") or []
                text = next(
                    (
                        item.get("text", "")
                        for item in content
                        if item.get("type") == "text"
                    ),
                    "",
                )
                if body.get("error") or result.get("isError") or text.startswith(
                    ("错误:", "Error:")
                ):
                    raise RuntimeError(text or str(body.get("error")))
                return body
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                last_error = exc
                if attempt < self._config.retry_attempts:
                    time.sleep(self._config.request_pause_seconds * attempt)
        assert last_error is not None
        raise RuntimeError(
            f"Tushare MCP request failed after {self._config.retry_attempts} attempts: "
            f"{last_error}"
        ) from last_error

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
        if request.start_date is not None or request.end_date is not None:
            params = {
                "ts_code": self._market.index_codes,
                "start_date": self._date(request.start_date or request.as_of_date),
                "end_date": self._date(request.end_date or request.as_of_date),
            }
            frames = [
                self._call(
                    self._client.index_daily,
                    ts_code=code,
                    start_date=params["start_date"],
                    end_date=params["end_date"],
                )
                for code in self._market.index_codes
            ]
        else:
            trade_date = self._date(request.as_of_date)
            params = {
                "ts_code": self._market.index_codes,
                "trade_date": trade_date,
            }
            frames = [
                self._call(
                    self._client.index_daily,
                    ts_code=code,
                    trade_date=trade_date,
                )
                for code in self._market.index_codes
            ]
        frame = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        return frame, params

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

    def _fetch_industry_members(
        self,
        request: FetchRequest,
    ) -> tuple[pl.DataFrame, dict[str, Any]]:
        classifications = self._call(
            self._client.index_classify,
            level="L1",
            src="SW2021",
        )
        if "index_code" not in classifications.columns:
            raise ValueError("Industry classification response is missing index_code.")
        industry_codes = classifications.get_column("index_code").drop_nulls().to_list()
        frames = [
            self._call(
                self._client.index_member_all,
                l1_code=code,
                is_new=is_new,
            )
            for code in industry_codes
            for is_new in ("Y", "N")
        ]
        frame = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        return frame, {
            "classification": "SW2021",
            "level": "L1",
            "industry_codes": industry_codes,
            "member_statuses": ["Y", "N"],
            "as_of_date": self._date(request.as_of_date),
        }

    def _fetch_daily_basic(self, request: FetchRequest) -> tuple[pl.DataFrame, dict[str, Any]]:
        fields = (
            "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,"
            "pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,"
            "free_share,total_mv,circ_mv"
        )
        params = {"trade_date": self._date(request.as_of_date), "fields": fields}
        return self._call(self._client.daily_basic, **params), params

    def _fetch_stock_limit(self, request: FetchRequest) -> tuple[pl.DataFrame, dict[str, Any]]:
        params = {
            "trade_date": self._date(request.as_of_date),
            "fields": "ts_code,trade_date,pre_close,up_limit,down_limit",
        }
        return self._call(self._client.stk_limit, **params), params

    def _fetch_financial_indicators(
        self, request: FetchRequest
    ) -> tuple[pl.DataFrame, dict[str, Any]]:
        if not request.periods:
            raise ValueError("Financial indicator fetch requires at least one report period.")
        fields = (
            "ts_code,ann_date,end_date,eps,ocfps,roe,roe_dt,roa,roic,"
            "grossprofit_margin,netprofit_margin,debt_to_assets,current_ratio,"
            "q_sales_yoy,q_netprofit_yoy,dt_netprofit_yoy,or_yoy,ocf_yoy,"
            "update_flag"
        )
        frames = [
            self._call(
                self._client.fina_indicator_vip,
                period=period,
                fields=fields,
            )
            for period in request.periods
        ]
        frame = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        return frame, {"periods": list(request.periods), "fields": fields}
