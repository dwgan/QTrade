# QTrade

QTrade 是一个面向个人研究的 A 股日线市场分析与决策辅助项目。目前已提供数据更新、历史回填、质量检查、市场状态、行业与大小盘风格分析。

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

为市场分析准备历史数据：

```powershell
qtrade data backfill --start 2025-01-01 --end 2026-07-24
```

回填命令先读取交易日历，只处理开市日期；已经完整落盘的日期会自动跳过，可以在中断后重跑。实际可获取的数据范围和调用频率取决于 Tushare 账户权限。

生成市场分析：

```powershell
qtrade analyze market --date 2026-07-24
```

市场分析结果写入 `reports/market/YYYY-MM-DD`。至少需要约 120 个交易日的指数和股票日线数据；历史不足时仍会说明具体缺口，但不会输出市场温度。

生成行业与风格分析前，需要保存分析日期对应的股票基础信息：

```powershell
qtrade data update --date 2026-07-24 --datasets security_master
qtrade analyze industry --date 2026-07-24
```

行业分析结果写入 `reports/industry/YYYY-MM-DD`。

准备多因子选股需要的季度财务快照：

```powershell
qtrade data financials `
  --date 2026-07-24 `
  --periods 20250331,20250630,20250930,20251231,20260331

qtrade data update `
  --date 2026-07-24 `
  --datasets security_master,daily_basic,stock_limit

qtrade analyze factors --date 2026-07-24
```

全市场季度财务命令使用 Tushare `fina_indicator_vip`，需要相应积分权限。分析结果写入 `reports/factors/YYYY-MM-DD`，包括候选报告和完整排名 Parquet。

积累历史因子排名后，可以运行因子有效性检验和候选组合回测：

```powershell
qtrade research factors --start 2025-01-01 --end 2025-12-31
qtrade backtest candidates --start 2025-01-01 --end 2025-12-31
```

研究结果写入 `reports/research`。规则与限制见
[因子研究与组合回测](./docs/10-因子研究与组合回测.md)。

## 数据位置

默认配置位于 `config/base.yaml`：

- `data/raw`：供应商原始结果
- `data/curated`：去重、排序后的标准化结果
- `data/snapshots`：每日更新清单
- `reports/data-quality`：数据质量报告

相同日期和数据集重复更新时采用原子替换，不追加重复记录。
