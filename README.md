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

如果使用兼容 Tushare SDK 的自定义 API 地址，可额外设置：

```powershell
$env:TUSHARE_API_URL = "https://your-compatible-endpoint.example/api"
```

未设置 `TUSHARE_API_URL` 时使用 Tushare SDK 的默认官方地址。

真实 token 不应写入配置文件或提交到 Git。

## 快速开始

推荐通过本地操作界面使用：

Windows 用户可以直接双击项目根目录的 `start_qtrade.cmd`。

也可以在 PowerShell 中启动：

```powershell
.\.venv\Scripts\python.exe -m qtrade ui
```

浏览器会自动打开 `http://127.0.0.1:8765`。在页面中可以：

- 选择已有报告日期；
- 联网更新数据并生成完整研究报告；
- 使用已有数据快速重算；
- 查看市场风险、行业排名和候选股票；
- 编辑自选股并查看每日状态；
- 查看后台任务进度、日志和数据质量状态。

界面只监听本机地址，不上传 Token，不连接券商，也不执行交易。详细说明见
[本地操作界面](./docs/14-本地操作界面.md)。

以下命令行方式仍然保留，适合自动化或故障排查。

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

多年研究数据建议分层回填：

```powershell
qtrade data backfill `
  --start 2015-01-01 `
  --end 2026-07-24 `
  --datasets daily_prices,adjust_factors,stock_limit

qtrade data index-backfill `
  --start 2015-01-01 `
  --end 2026-07-24

qtrade data backfill `
  --start 2015-01-01 `
  --end 2026-07-24 `
  --datasets daily_basic `
  --frequency month_end
```

指数行情按指数整段抓取；月末策略的估值数据只抓取每月最后一个交易日。

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

qtrade analyze factors --date 2026-07-24 --origin reconstructed
```

全市场季度财务命令使用 Tushare `fina_indicator_vip`，需要相应积分权限。分析结果写入 `reports/factors/YYYY-MM-DD`，包括候选报告和完整排名 Parquet。历史补算使用 `reconstructed`；只有当天按冻结逻辑真实产生的信号才可标记为 `live_observed`。

积累历史因子排名后，可以运行因子有效性检验和候选组合回测：

```powershell
qtrade research factors --start 2025-01-01 --end 2025-12-31
qtrade backtest candidates `
  --start 2025-01-01 `
  --end 2025-12-31 `
  --split-date 2025-09-01
```

研究结果写入 `reports/research`。规则与限制见
[因子研究与组合回测](./docs/10-因子研究与组合回测.md)。
成交限制和稳健性口径见
[回测成交约束与稳健性](./docs/11-回测成交约束与稳健性.md)。

在 `observation.watchlist_symbols` 配置自选股后，可生成候选变化、自选股和影子组合日报：

```powershell
qtrade observe daily --date 2026-07-24
```

结果写入 `reports/observations/YYYY-MM-DD`，详细说明见
[每日观察与影子组合](./docs/12-每日观察与影子组合.md)。

收盘后可以通过一条命令更新数据并生成全部分析、观察报告和本地看板：

```powershell
qtrade pipeline daily --date 2026-07-24
```

使用已有数据重跑：

```powershell
qtrade pipeline daily --date 2026-07-24 --skip-data
```

只重新生成静态看板：

```powershell
qtrade dashboard build --date 2026-07-24
```

详细运行和失败语义见
[日度流水线与本地看板](./docs/13-日度流水线与本地看板.md)。

## 数据位置

默认配置位于 `config/base.yaml`：

- `data/raw`：供应商原始结果
- `data/curated`：去重、排序后的标准化结果
- `data/snapshots`：每日更新清单
- `reports/data-quality`：数据质量报告

相同日期和数据集重复更新时采用原子替换，不追加重复记录。
