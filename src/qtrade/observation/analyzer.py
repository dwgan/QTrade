from __future__ import annotations

import polars as pl

from qtrade.config import ObservationConfig
from qtrade.observation.models import CandidateChange, WatchlistItem


class ObservationAnalyzer:
    REQUIRED_COLUMNS = {"rank", "ts_code", "name", "industry", "score"}

    def __init__(self, config: ObservationConfig) -> None:
        self.config = config

    def _rows(self, frame: pl.DataFrame, label: str) -> dict[str, dict]:
        if missing := self.REQUIRED_COLUMNS - set(frame.columns):
            raise ValueError(
                f"{label} ranking is missing columns: {', '.join(sorted(missing))}"
            )
        return {
            str(row["ts_code"]): row
            for row in frame.select(*sorted(self.REQUIRED_COLUMNS)).to_dicts()
        }

    @staticmethod
    def _change(
        code: str,
        change_type: str,
        current: dict[str, dict],
        previous: dict[str, dict],
    ) -> CandidateChange:
        current_row = current.get(code)
        previous_row = previous.get(code)
        reference = current_row or previous_row or {}
        current_rank = int(current_row["rank"]) if current_row is not None else None
        previous_rank = int(previous_row["rank"]) if previous_row is not None else None
        return CandidateChange(
            ts_code=code,
            name=str(reference.get("name") or ""),
            industry=str(reference.get("industry") or "unknown"),
            change_type=change_type,
            previous_rank=previous_rank,
            current_rank=current_rank,
            rank_change=(
                previous_rank - current_rank
                if previous_rank is not None and current_rank is not None
                else None
            ),
            score=float(current_row["score"]) if current_row is not None else None,
        )

    def analyze(
        self,
        current_frame: pl.DataFrame,
        previous_frame: pl.DataFrame | None,
    ) -> tuple[
        list[CandidateChange],
        list[CandidateChange],
        list[CandidateChange],
        list[WatchlistItem],
    ]:
        current = self._rows(current_frame, "Current")
        previous = self._rows(previous_frame, "Previous") if previous_frame is not None else {}
        current_top = {
            code
            for code, row in current.items()
            if int(row["rank"]) <= self.config.candidate_count
        }
        previous_top = {
            code
            for code, row in previous.items()
            if int(row["rank"]) <= self.config.candidate_count
        }
        entered = (
            [
                self._change(code, "entered", current, previous)
                for code in sorted(
                    current_top - previous_top,
                    key=lambda item: current[item]["rank"],
                )
            ]
            if previous_frame is not None
            else []
        )
        exited = (
            [
                self._change(code, "exited", current, previous)
                for code in sorted(
                    previous_top - current_top,
                    key=lambda item: previous[item]["rank"],
                )
            ]
            if previous_frame is not None
            else []
        )
        movers = (
            [
                self._change(
                    code,
                    "improved" if change > 0 else "deteriorated",
                    current,
                    previous,
                )
                for code in current.keys() & previous.keys()
                if (change := int(previous[code]["rank"]) - int(current[code]["rank"]))
                != 0
            ]
            if previous_frame is not None
            else []
        )
        movers.sort(
            key=lambda item: (
                -abs(item.rank_change or 0),
                item.current_rank or 10**9,
                item.ts_code,
            )
        )

        watchlist: list[WatchlistItem] = []
        for code in self.config.watchlist_symbols:
            current_row = current.get(code)
            previous_row = previous.get(code)
            current_rank = int(current_row["rank"]) if current_row is not None else None
            previous_rank = int(previous_row["rank"]) if previous_row is not None else None
            reference = current_row or previous_row or {}
            watchlist.append(
                WatchlistItem(
                    ts_code=code,
                    name=str(reference.get("name") or ""),
                    industry=str(reference.get("industry") or "unknown"),
                    status=(
                        "candidate"
                        if current_rank is not None
                        and current_rank <= self.config.candidate_count
                        else "ranked"
                        if current_rank is not None
                        else "not_ranked"
                    ),
                    current_rank=current_rank,
                    previous_rank=previous_rank,
                    rank_change=(
                        previous_rank - current_rank
                        if previous_rank is not None and current_rank is not None
                        else None
                    ),
                    score=(
                        float(current_row["score"]) if current_row is not None else None
                    ),
                )
            )
        return entered, exited, movers[: self.config.rank_mover_count], watchlist
