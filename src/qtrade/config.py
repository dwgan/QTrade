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
    request_pause_seconds: float = Field(default=0.2, ge=0)
    retry_attempts: int = Field(default=3, ge=1, le=10)

    def token(self) -> str:
        value = os.getenv(self.token_env, "").strip()
        if not value:
            raise RuntimeError(
                f"Missing provider credential: set environment variable {self.token_env}."
            )
        return value


class MarketConfig(BaseModel):
    exchange: str = "SSE"
    index_codes: list[str] = Field(default_factory=lambda: ["000300.SH", "000905.SH", "000852.SH"])

    @field_validator("index_codes")
    @classmethod
    def unique_index_codes(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class UpdateConfig(BaseModel):
    datasets: list[str] = Field(default_factory=list)


class ValidationConfig(BaseModel):
    minimum_daily_rows: int = Field(default=100, ge=0)
    fail_on_warning: bool = False


class AppConfig(BaseModel):
    app: AppInfo = Field(default_factory=AppInfo)
    paths: PathConfig = Field(default_factory=PathConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)
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
