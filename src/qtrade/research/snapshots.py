from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

import polars as pl


class FactorSnapshotStore:
    """Read immutable ranking outputs produced by daily factor analysis."""

    def __init__(self, reports_root: Path) -> None:
        self.root = Path(reports_root) / "factors"

    def available_dates(self, start_date: date, end_date: date) -> list[date]:
        if start_date > end_date:
            raise ValueError("Start date must not be after end date.")
        if not self.root.exists():
            return []
        values: list[date] = []
        for directory in self.root.iterdir():
            try:
                snapshot_date = date.fromisoformat(directory.name)
            except ValueError:
                continue
            if start_date <= snapshot_date <= end_date and self._ranking_path(
                snapshot_date
            ) is not None:
                values.append(snapshot_date)
        return sorted(values)

    def read(self, snapshot_date: date) -> pl.DataFrame:
        path = self._ranking_path(snapshot_date)
        if path is None:
            raise FileNotFoundError(
                f"Factor ranking snapshot not found for {snapshot_date}."
            )
        self._verify_version(path.parent)
        return pl.read_parquet(path)

    def manifest(self, snapshot_date: date) -> dict | None:
        path = self._ranking_path(snapshot_date)
        if path is None:
            raise FileNotFoundError(
                f"Factor ranking snapshot not found for {snapshot_date}."
            )
        manifest_path = path.parent / "manifest.json"
        if not manifest_path.exists():
            return None
        self._verify_version(path.parent)
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    def _ranking_path(self, snapshot_date: date) -> Path | None:
        directory = self.root / snapshot_date.isoformat()
        latest_path = directory / "latest.json"
        if latest_path.exists():
            try:
                latest = json.loads(latest_path.read_text(encoding="utf-8"))
                signal_id = str(latest["signal_id"])
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ValueError(f"Invalid factor latest pointer: {latest_path}") from exc
            if not re.fullmatch(r"[0-9a-f]{64}", signal_id):
                raise ValueError(f"Invalid signal id in latest pointer: {latest_path}")
            version_path = directory / "versions" / signal_id / "rankings.parquet"
            if not version_path.exists():
                raise FileNotFoundError(
                    f"Latest factor signal version is incomplete: {version_path}"
                )
            return version_path
        legacy_path = directory / "rankings.parquet"
        return legacy_path if legacy_path.exists() else None

    @classmethod
    def _verify_version(cls, directory: Path) -> None:
        manifest_path = directory / "manifest.json"
        if not manifest_path.exists():
            return
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for name, expected in manifest.get("files", {}).items():
            path = directory / name
            if not path.exists() or cls._file_hash(path) != expected:
                raise ValueError(f"Immutable signal file hash mismatch: {path}")

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
