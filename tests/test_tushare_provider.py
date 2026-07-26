from datetime import date

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
