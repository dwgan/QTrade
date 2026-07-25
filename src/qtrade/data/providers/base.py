from __future__ import annotations

from typing import Protocol

from qtrade.domain import DataBatch, Dataset, FetchRequest


class DataProvider(Protocol):
    @property
    def name(self) -> str: ...

    def fetch(self, dataset: Dataset, request: FetchRequest) -> DataBatch: ...
