from datetime import date
from typing import Any

import pandas as pd

from qtrade.config import MarketConfig, ProviderConfig
from qtrade.data.providers.tushare import TushareProvider
from qtrade.domain import Dataset, FetchRequest


class FakeClient:
    def daily(self, **kwargs):
        assert kwargs == {"trade_date": "20260724"}
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": ["20260724"],
                "open": [10.0],
                "high": [10.5],
                "low": [9.8],
                "close": [10.2],
                "pre_close": [9.9],
                "vol": [100.0],
                "amount": [1000.0],
            }
        )


class FactorDataClient:
    def daily_basic(self, **kwargs):
        assert kwargs["trade_date"] == "20260724"
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": ["20260724"],
                "pe_ttm": [10.0],
            }
        )

    def fina_indicator_vip(self, **kwargs):
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "ann_date": ["20260430"],
                "end_date": [kwargs["period"]],
                "roe": [10.0],
            }
        )


class StockLimitClient:
    def stk_limit(self, **kwargs):
        assert kwargs == {
            "trade_date": "20260724",
            "fields": "ts_code,trade_date,pre_close,up_limit,down_limit",
        }
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": ["20260724"],
                "pre_close": [10.0],
                "up_limit": [11.0],
                "down_limit": [9.0],
            }
        )


class IndustryClient:
    def index_classify(self, **kwargs):
        assert kwargs == {"level": "L1", "src": "SW2021"}
        return pd.DataFrame({"index_code": ["801010.SI", "801020.SI"]})

    def index_member_all(self, **kwargs):
        return pd.DataFrame(
            {
                "l1_code": [kwargs["l1_code"]],
                "l1_name": ["Industry"],
                "ts_code": ["000001.SZ"],
                "in_date": ["20210101"],
                "out_date": [None],
            }
        )


def test_tushare_daily_adapter_converts_to_polars() -> None:
    provider = TushareProvider(
        ProviderConfig(request_pause_seconds=0),
        MarketConfig(),
        client=FakeClient(),
    )

    batch = provider.fetch(Dataset.DAILY_PRICES, FetchRequest(as_of_date=date(2026, 7, 24)))

    assert batch.provider == "tushare"
    assert batch.frame.height == 1
    assert batch.frame.get_column("ts_code").item() == "000001.SZ"


def test_tushare_factor_data_adapters() -> None:
    provider = TushareProvider(
        ProviderConfig(request_pause_seconds=0),
        MarketConfig(),
        client=FactorDataClient(),
    )

    basic = provider.fetch(Dataset.DAILY_BASIC, FetchRequest(as_of_date=date(2026, 7, 24)))
    financials = provider.fetch(
        Dataset.FINANCIAL_INDICATORS,
        FetchRequest(
            as_of_date=date(2026, 7, 24),
            periods=("20251231", "20260331"),
        ),
    )

    assert basic.frame.height == 1
    assert financials.frame.height == 2
    assert financials.request["periods"] == ["20251231", "20260331"]


def test_tushare_stock_limit_requests_required_fields() -> None:
    provider = TushareProvider(
        ProviderConfig(request_pause_seconds=0),
        MarketConfig(),
        client=StockLimitClient(),
    )

    batch = provider.fetch(
        Dataset.STOCK_LIMIT,
        FetchRequest(as_of_date=date(2026, 7, 24)),
    )

    assert batch.frame.columns == [
        "ts_code",
        "trade_date",
        "pre_close",
        "up_limit",
        "down_limit",
    ]


def test_tushare_fetches_effective_dated_industry_members() -> None:
    provider = TushareProvider(
        ProviderConfig(request_pause_seconds=0),
        MarketConfig(),
        client=IndustryClient(),
    )

    batch = provider.fetch(
        Dataset.INDUSTRY_MEMBERS,
        FetchRequest(as_of_date=date(2026, 7, 24)),
    )

    assert batch.frame.height == 4
    assert batch.request["industry_codes"] == ["801010.SI", "801020.SI"]
    assert batch.request["member_statuses"] == ["Y", "N"]


def test_tushare_client_uses_optional_api_gateway(monkeypatch) -> None:
    import tushare as ts

    client = object()
    wrapper = type("Client", (), {})()
    monkeypatch.setattr(ts, "pro_api", lambda token: wrapper if token == "test-token" else client)
    monkeypatch.setenv("TEST_TOKEN", "test-token")
    monkeypatch.setenv("TEST_API_URL", "https://example.test/api")

    provider = TushareProvider(
        ProviderConfig(
            token_env="TEST_TOKEN",
            api_url_env="TEST_API_URL",
            request_pause_seconds=0,
        ),
        MarketConfig(),
    )

    assert provider._client._DataApi__http_url == "https://example.test/api"


class FakeMcpResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


def test_tushare_ft_limit_can_page_through_documented_mcp_gateway(
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []
    failed_once = False

    def fake_post(url, *, json, headers, timeout):
        nonlocal failed_once
        assert url == "https://example.test/mcp/token=secret"
        assert headers["Accept"] == "application/json, text/event-stream"
        assert timeout == 60
        arguments = json["params"]["arguments"]
        calls.append(arguments)
        offset = arguments["params"]["offset"]
        if not failed_once:
            failed_once = True
            return FakeMcpResponse(
                {
                    "result": {
                        "content": [{"type": "text", "text": "错误: context cancelled"}],
                    }
                }
            )
        rows = {
            0: "20260724,CU2608.SHF,85000,71000,10,CU,SHFE",
            1: "20260724,AL2608.SHF,22000,18000,10,AL,SHFE",
        }
        text = (
            "trade_date,ts_code,up_limit,down_limit,m_ratio,cont,exchange\n"
            + rows[offset]
            + "\n... 共 2 条，仅显示前 1 条"
        )
        return FakeMcpResponse(
            {
                "result": {
                    "content": [{"type": "text", "text": text}],
                }
            }
        )

    monkeypatch.setenv("TEST_MCP_URL", "https://example.test/mcp/token=secret")
    monkeypatch.setattr("requests.post", fake_post)
    provider = TushareProvider(
        ProviderConfig(
            mcp_url_env="TEST_MCP_URL",
            request_pause_seconds=0,
            mcp_request_pause_seconds=0,
        ),
        MarketConfig(),
        client=object(),
    )

    frame = provider.query(
        "ft_limit",
        trade_date="20260724",
        fields="trade_date,ts_code,up_limit,down_limit,m_ratio,cont,exchange",
        mcp_page_size=1,
    )

    assert frame.get_column("ts_code").to_list() == ["CU2608.SHF", "AL2608.SHF"]
    assert frame.get_column("up_limit").to_list() == [85000.0, 22000.0]
    assert frame.get_column("source_transport").unique().to_list() == ["mcp_query_data"]
    assert frame.get_column("source_reported_total").unique().to_list() == [2]
    assert [call["params"]["offset"] for call in calls] == [0, 0, 1]
    assert all(call["api_name"] == "ft_limit" for call in calls)


def test_tushare_conversion_normalizes_nan_in_mixed_columns() -> None:
    source = pd.DataFrame(
        {
            "end_date": [float("nan"), "20260712"],
            "change_reason": ["renamed", float("nan")],
            "numeric": [1.5, float("nan")],
        }
    )

    frame = TushareProvider._to_polars(source)

    assert frame.get_column("end_date").to_list() == [None, "20260712"]
    assert frame.get_column("change_reason").to_list() == ["renamed", None]
    assert frame.get_column("numeric").to_list() == [1.5, None]


def test_tushare_futures_query_is_whitelisted() -> None:
    class FuturesClient:
        def fut_daily(self, **kwargs):
            return pd.DataFrame(
                {
                    "ts_code": ["CU2608.SHF"],
                    "trade_date": [kwargs["trade_date"]],
                }
            )

    provider = TushareProvider(
        ProviderConfig(request_pause_seconds=0),
        MarketConfig(),
        client=FuturesClient(),
    )

    frame = provider.query("fut_daily", trade_date="20260724")

    assert frame.get_column("ts_code").item() == "CU2608.SHF"

    import pytest

    with pytest.raises(ValueError, match="Unsupported Tushare query"):
        provider.query("stock_basic")
