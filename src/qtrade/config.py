from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class AppInfo(BaseModel):
    name: str = "qtrade"
    timezone: str = "Asia/Shanghai"


class PathConfig(BaseModel):
    raw: Path = Path("data/raw")
    curated: Path = Path("data/curated")
    snapshots: Path = Path("data/snapshots")
    reports: Path = Path("reports")
    runtime: Path = Path("runtime")

    def resolve(self, root: Path) -> PathConfig:
        values = {
            name: path if path.is_absolute() else root / path
            for name, path in self.model_dump().items()
        }
        return PathConfig(**values)

    def create(self) -> None:
        for path in self.model_dump().values():
            Path(path).mkdir(parents=True, exist_ok=True)


class ProviderConfig(BaseModel):
    name: str = "tushare"
    token_env: str = "TUSHARE_TOKEN"
    api_url_env: str = "TUSHARE_API_URL"
    request_pause_seconds: float = Field(default=0.2, ge=0)
    retry_attempts: int = Field(default=3, ge=1, le=10)
    parallel_requests: int = Field(default=3, ge=1, le=5)
    backfill_parallel_dates: int = Field(default=2, ge=1, le=2)

    def token(self) -> str:
        value = os.getenv(self.token_env, "").strip()
        if not value:
            raise RuntimeError(
                f"Missing provider credential: set environment variable {self.token_env}."
            )
        return value

    def api_url(self) -> str | None:
        return os.getenv(self.api_url_env, "").strip() or None


class MarketConfig(BaseModel):
    exchange: str = "SSE"
    index_codes: list[str] = Field(default_factory=lambda: ["000300.SH", "000905.SH", "000852.SH"])
    primary_index_code: str = "000300.SH"
    history_calendar_days: int = Field(default=500, ge=250)
    minimum_history_days: int = Field(default=120, ge=60)
    minimum_breadth_stocks: int = Field(default=100, ge=1)
    attack_threshold: float = Field(default=70, ge=0, le=100)
    balanced_threshold: float = Field(default=50, ge=0, le=100)
    defensive_threshold: float = Field(default=30, ge=0, le=100)

    @field_validator("index_codes")
    @classmethod
    def unique_index_codes(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @field_validator("defensive_threshold")
    @classmethod
    def ordered_thresholds(cls, value: float, info) -> float:
        data = info.data
        if (
            "attack_threshold" in data
            and "balanced_threshold" in data
            and not data["attack_threshold"] > data["balanced_threshold"] > value
        ):
            raise ValueError("Market thresholds must satisfy attack > balanced > defensive.")
        return value


class UpdateConfig(BaseModel):
    datasets: list[str] = Field(default_factory=list)


class IndustryConfig(BaseModel):
    classification_column: str = "industry"
    minimum_stocks: int = Field(default=5, ge=2)
    top_count: int = Field(default=10, ge=1)
    history_calendar_days: int = Field(default=180, ge=100)
    style_pairs: dict[str, tuple[str, str]] = Field(
        default_factory=lambda: {
            "mid_vs_large": ("000905.SH", "000300.SH"),
            "small_vs_large": ("000852.SH", "000300.SH"),
        }
    )


class FactorConfig(BaseModel):
    history_calendar_days: int = Field(default=500, ge=250)
    minimum_history_days: int = Field(default=121, ge=121)
    minimum_listing_days: int = Field(default=365, ge=0)
    liquidity_exclusion_percentile: float = Field(default=0.20, ge=0, lt=0.5)
    candidate_count: int = Field(default=30, ge=1)
    max_candidates_per_industry: int = Field(default=5, ge=1)
    universe_index_codes: list[str] = Field(
        default_factory=lambda: ["000300.SH", "000905.SH"]
    )
    exclude_industry_keywords: list[str] = Field(
        default_factory=lambda: ["银行", "保险", "证券", "多元金融"]
    )
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "quality": 0.35,
            "momentum": 0.30,
            "value": 0.20,
            "low_risk": 0.15,
        }
    )

    @field_validator("weights")
    @classmethod
    def valid_weights(cls, value: dict[str, float]) -> dict[str, float]:
        required = {"quality", "momentum", "value", "low_risk"}
        if set(value) != required:
            raise ValueError(f"Factor weights must contain exactly: {sorted(required)}")
        if any(weight < 0 for weight in value.values()):
            raise ValueError("Factor weights must be non-negative.")
        total = sum(value.values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError("Factor weights must sum to 1.")
        return value

    @field_validator("universe_index_codes")
    @classmethod
    def unique_universe_index_codes(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip().upper() for item in value if item.strip()))
        if not normalized:
            raise ValueError("Factor universe requires at least one index code.")
        return normalized


class ResearchConfig(BaseModel):
    forward_horizon_days: int = Field(default=20, ge=1)
    quantiles: int = Field(default=5, ge=2, le=10)
    minimum_cross_section: int = Field(default=10, ge=2)


class FuturesConfig(BaseModel):
    exchanges: list[str] = Field(
        default_factory=lambda: [
            "CFFEX",
            "SHFE",
            "INE",
            "DCE",
            "CZCE",
            "GFEX",
        ]
    )
    audit_minimum_daily_volume: float = Field(default=1000, ge=0)
    audit_minimum_open_interest: float = Field(default=1000, ge=0)
    excluded_product_codes: list[str] = Field(
        default_factory=lambda: ["SCTAS", "L_F", "PP_F", "V_F"]
    )
    update_datasets: list[str] = Field(
        default_factory=lambda: [
            "futures_contracts",
            "futures_contract_rules",
            "futures_daily",
            "futures_settlements",
            "futures_mappings",
            "futures_limits",
            "futures_calendar",
        ]
    )
    roll_confirmation_days: int = Field(default=2, ge=1, le=10)
    roll_expiry_buffer_calendar_days: int = Field(default=15, ge=1, le=90)
    universe_lookback_days: int = Field(default=20, ge=1, le=250)
    universe_min_history_days: int = Field(default=20, ge=1, le=250)
    universe_min_contracts: int = Field(default=2, ge=1, le=12)
    universe_minimum_daily_volume: float = Field(default=1000, ge=0)
    universe_minimum_open_interest: float = Field(default=1000, ge=0)
    universe_minimum_daily_amount: float = Field(default=0, ge=0)
    continuous_max_abs_return: float = Field(default=0.25, gt=0, le=1)
    execution_slippage_ticks: int = Field(default=1, ge=0, le=100)
    margin_call_buffer: float = Field(default=1.05, ge=1, le=2)
    stress_margin_multiplier: float = Field(default=1.5, ge=1, le=5)

    @field_validator("exchanges")
    @classmethod
    def valid_exchanges(cls, value: list[str]) -> list[str]:
        supported = {"CFFEX", "SHFE", "INE", "DCE", "CZCE", "GFEX"}
        normalized = list(
            dict.fromkeys(item.strip().upper() for item in value if item.strip())
        )
        if not normalized:
            raise ValueError("Futures audit requires at least one exchange.")
        if unknown := set(normalized) - supported:
            raise ValueError(
                "Unsupported futures exchanges: " + ", ".join(sorted(unknown))
            )
        return normalized

    @field_validator("excluded_product_codes")
    @classmethod
    def normalized_excluded_products(cls, value: list[str]) -> list[str]:
        return list(
            dict.fromkeys(item.strip().upper() for item in value if item.strip())
        )


class BacktestConfig(BaseModel):
    initial_capital: float = Field(default=1_000_000, gt=0)
    transaction_cost_rate: float = Field(default=0.0015, ge=0, lt=0.1)
    slippage_rate: float = Field(default=0.0005, ge=0, lt=0.1)
    annual_risk_free_rate: float = Field(default=0.0, ge=0, lt=1)
    candidate_count: int = Field(default=20, ge=1)
    max_candidates_per_industry: int = Field(default=5, ge=1)
    benchmark_code: str = "000300.SH"
    sample_split_ratio: float = Field(default=0.70, gt=0, lt=1)
    cost_sensitivity_multipliers: list[float] = Field(
        default_factory=lambda: [0.0, 1.0, 2.0]
    )

    @field_validator("cost_sensitivity_multipliers")
    @classmethod
    def valid_cost_multipliers(cls, value: list[float]) -> list[float]:
        if not value or any(item < 0 for item in value):
            raise ValueError("Cost sensitivity multipliers must be non-empty and non-negative.")
        return list(dict.fromkeys(value))


class ObservationConfig(BaseModel):
    watchlist_symbols: list[str] = Field(default_factory=list)
    candidate_count: int = Field(default=20, ge=1)
    rank_mover_count: int = Field(default=10, ge=1)
    shadow_lookback_calendar_days: int = Field(default=365, ge=30)

    @field_validator("watchlist_symbols")
    @classmethod
    def normalized_watchlist(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip().upper() for item in value if item.strip()))


class ValidationConfig(BaseModel):
    minimum_daily_rows: int = Field(default=100, ge=0)
    fail_on_warning: bool = False


class AppConfig(BaseModel):
    app: AppInfo = Field(default_factory=AppInfo)
    paths: PathConfig = Field(default_factory=PathConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)
    industry: IndustryConfig = Field(default_factory=IndustryConfig)
    factors: FactorConfig = Field(default_factory=FactorConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    futures: FuturesConfig = Field(default_factory=FuturesConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    observation: ObservationConfig = Field(default_factory=ObservationConfig)
    update: UpdateConfig = Field(default_factory=UpdateConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    project_root: Path = Field(default=Path.cwd(), exclude=True)


def load_config(path: str | Path = "config/base.yaml") -> AppConfig:
    config_path = Path(path).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as stream:
        raw: dict[str, Any] = yaml.safe_load(stream) or {}

    # A config under <project>/config is resolved from <project>.
    project_root = config_path.parent.parent
    config = AppConfig.model_validate({**raw, "project_root": project_root})
    config.paths = config.paths.resolve(project_root)
    return config
