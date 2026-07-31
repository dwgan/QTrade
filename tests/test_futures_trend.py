from __future__ import annotations

import math
from datetime import date, timedelta

import polars as pl

from qtrade.futures.trend import FuturesTrendEngine, FuturesTrendProtocol


def market_inputs() -> tuple[date, date, pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    dates = [date(2025, 1, 1) + timedelta(days=index) for index in range(132)]
    prices = [100.0]
    for index in range(1, len(dates)):
        prices.append(prices[-1] * (1.006 if index % 2 else 1.002))
    signal_date = dates[-2]
    eligible_date = dates[-1]
    continuous = pl.DataFrame(
        {
            "trade_date": [value.isoformat() for value in dates],
            "product_code": ["CU"] * len(dates),
            "continuous_index": prices,
        }
    )
    universe = pl.DataFrame(
        {
            "trade_date": [signal_date.isoformat()],
            "product_code": ["CU"],
            "eligible": [True],
        }
    )
    roll_schedule = pl.DataFrame(
        {
            "decision_date": [signal_date.isoformat()],
            "effective_date": [eligible_date.isoformat()],
            "product_code": ["CU"],
            "selected_contract": ["CU2607.SHF"],
            "universe_eligible": [True],
        }
    )
    contracts = pl.DataFrame(
        {
            "trade_date": [signal_date.isoformat()],
            "contract_code": ["CU2607.SHF"],
            "settle": [800.0],
            "multiplier": [5.0],
            "sector": ["base_metals"],
            "long_margin_rate": [0.10],
            "short_margin_rate": [0.12],
        }
    )
    return signal_date, eligible_date, continuous, universe, roll_schedule, contracts


def test_trend_target_uses_frozen_t_plus_one_contract_and_whole_lots() -> None:
    signal_date, eligible_date, continuous, universe, roll_schedule, contracts = market_inputs()
    protocol = FuturesTrendProtocol()

    result = FuturesTrendEngine(protocol).generate(
        signal_date=signal_date,
        eligible_date=eligible_date,
        equity=10_000_000,
        continuous=continuous,
        universe=universe,
        roll_schedule=roll_schedule,
        contracts=contracts,
    )

    target = result.targets[0]
    assert target.contract_code == "CU2607.SHF"
    assert target.sector == "base_metals"
    assert target.signal_strength == 1.0
    assert target.status == "targeted"
    assert target.target_signed_lots > 0
    assert target.unconstrained_signed_lots >= target.target_signed_lots
    assert target.limit_reasons == ()
    assert target.target_signed_lots == math.floor(
        target.allocated_daily_risk / target.one_lot_daily_risk
    )
    assert target.eligible_date == eligible_date
    assert result.initial_margin <= 10_000_000 * 0.25
    assert result.stress_margin <= 10_000_000 * 0.50
    assert result.total_daily_risk > 0


def test_future_continuous_price_cannot_change_signal_date_target() -> None:
    signal_date, eligible_date, continuous, universe, roll_schedule, contracts = market_inputs()
    engine = FuturesTrendEngine(FuturesTrendProtocol())
    baseline = engine.generate(
        signal_date,
        eligible_date,
        10_000_000,
        continuous,
        universe,
        roll_schedule,
        contracts,
    )
    changed = continuous.with_columns(
        pl.when(pl.col("trade_date") == eligible_date.isoformat())
        .then(pl.lit(0.0001))
        .otherwise(pl.col("continuous_index"))
        .alias("continuous_index")
    )

    after_future_change = engine.generate(
        signal_date,
        eligible_date,
        10_000_000,
        changed,
        universe,
        roll_schedule,
        contracts,
    )

    assert after_future_change == baseline


def test_one_lot_above_risk_budget_reports_insufficient_capital() -> None:
    signal_date, eligible_date, continuous, universe, roll_schedule, contracts = market_inputs()

    result = FuturesTrendEngine(FuturesTrendProtocol()).generate(
        signal_date,
        eligible_date,
        1_000,
        continuous,
        universe,
        roll_schedule,
        contracts,
    )

    target = result.targets[0]
    assert target.status == "insufficient_capital"
    assert target.target_signed_lots == 0
    assert target.one_lot_daily_risk > target.allocated_daily_risk


def test_short_target_uses_short_margin_rate() -> None:
    signal_date, eligible_date, continuous, universe, roll_schedule, contracts = market_inputs()
    falling = continuous.with_columns((1 / pl.col("continuous_index")).alias("continuous_index"))

    result = FuturesTrendEngine(FuturesTrendProtocol()).generate(
        signal_date,
        eligible_date,
        10_000_000,
        falling,
        universe,
        roll_schedule,
        contracts,
    )

    target = result.targets[0]
    assert target.signal_strength == -1.0
    assert target.target_signed_lots < 0
    assert target.one_lot_initial_margin == 800.0 * 5.0 * 0.12


def test_previous_snapshot_buffers_small_safe_position_change() -> None:
    signal_date, eligible_date, continuous, universe, roll_schedule, contracts = market_inputs()
    engine = FuturesTrendEngine(FuturesTrendProtocol())
    baseline = engine.generate(
        signal_date,
        eligible_date,
        10_000_000,
        continuous,
        universe,
        roll_schedule,
        contracts,
    )
    desired_lots = baseline.targets[0].unconstrained_signed_lots

    buffered = engine.generate(
        signal_date,
        eligible_date,
        10_000_000,
        continuous,
        universe,
        roll_schedule,
        contracts,
        previous_targets={"CU": desired_lots - 1},
    )

    target = buffered.targets[0]
    assert target.buffer_applied
    assert target.buffered_signed_lots == desired_lots - 1
    assert target.target_signed_lots == desired_lots - 1
    assert target.status == "buffered"


def test_position_buffer_cannot_exceed_current_product_risk_budget() -> None:
    signal_date, eligible_date, continuous, universe, roll_schedule, contracts = market_inputs()
    engine = FuturesTrendEngine(FuturesTrendProtocol())
    baseline = engine.generate(
        signal_date,
        eligible_date,
        10_000_000,
        continuous,
        universe,
        roll_schedule,
        contracts,
    )
    desired_lots = baseline.targets[0].unconstrained_signed_lots

    result = engine.generate(
        signal_date,
        eligible_date,
        10_000_000,
        continuous,
        universe,
        roll_schedule,
        contracts,
        previous_targets={"CU": desired_lots + 1},
    )

    target = result.targets[0]
    assert not target.buffer_applied
    assert target.buffered_signed_lots == desired_lots
    assert target.target_signed_lots == desired_lots
