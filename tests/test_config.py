from pathlib import Path

from qtrade.config import load_config


def test_load_config_resolves_paths_from_project_root(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "test.yaml").write_text(
        """
paths:
  raw: local/raw
provider:
  token_env: TEST_TUSHARE_TOKEN
update:
  datasets:
    - daily_prices
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_dir / "test.yaml")

    assert config.project_root == tmp_path
    assert config.paths.raw == tmp_path / "local/raw"
    assert config.update.datasets == ["daily_prices"]
    assert config.provider.token_env == "TEST_TUSHARE_TOKEN"


def test_provider_token_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("TEST_TUSHARE_TOKEN", " secret ")
    from qtrade.config import ProviderConfig

    assert ProviderConfig(token_env="TEST_TUSHARE_TOKEN").token() == "secret"
