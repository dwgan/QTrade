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
