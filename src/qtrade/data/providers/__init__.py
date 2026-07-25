"""External market data providers."""

from qtrade.data.providers.base import DataProvider
from qtrade.data.providers.tushare import TushareProvider

__all__ = ["DataProvider", "TushareProvider"]
