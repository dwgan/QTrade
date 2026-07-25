# QTrade

QTrade 是一个面向个人研究的 A 股日线市场分析与决策辅助项目。第一阶段提供数据更新、标准化存储、质量检查和命令行入口。

详细设计见 [项目文档](./docs/README.md)。

## 环境

- Python 3.12+
- Tushare Pro token

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

设置凭据：

```powershell
$env:TUSHARE_TOKEN = "your-token"
```

真实 token 不应写入配置文件或提交到 Git。

## 快速开始

更新指定交易日的默认数据集：

```powershell
qtrade data update --date 2026-07-24
```

只更新部分数据集：

```powershell
qtrade data update --date 2026-07-24 --datasets daily_prices,adjust_factors
```

校验指定日期已经落盘的数据：

```powershell
qtrade data validate --date 2026-07-24
```

查看可用数据集：

```powershell
qtrade data datasets
```

首次获取长区间历史数据将在后续迭代中加入。目前的 `update` 命令以“按交易日增量更新”为核心。

## 数据位置

默认配置位于 `config/base.yaml`：

- `data/raw`：供应商原始结果
- `data/curated`：去重、排序后的标准化结果
- `data/snapshots`：每日更新清单
- `reports/data-quality`：数据质量报告

相同日期和数据集重复更新时采用原子替换，不追加重复记录。

