const state = {
  meta: null,
  overview: null,
  candidates: [],
  taskTimer: null,
  backtestMeta: null,
  selectedPartition: null,
  backtestTimer: null,
  positions: [],
};

const $ = (id) => document.getElementById(id);
const pct = (value, digits = 1) => value == null ? "—" : `${(Number(value) * 100).toFixed(digits)}%`;
const num = (value, digits = 1) => value == null ? "—" : Number(value).toFixed(digits);
const text = (value, fallback = "—") => value == null || value === "" ? fallback : String(value);
const escapeHtml = (value) => text(value, "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
})[char]);

const labels = {
  high_risk: "高风险",
  defensive: "防守",
  balanced: "均衡",
  attack: "进攻",
  trend_strengthening: "趋势增强",
  strong_continuation: "强势延续",
  high_level_divergence: "高位分化",
  neutral: "中性",
  trend_weakening: "趋势转弱",
  weakening: "趋势转弱",
  candidate: "候选",
  ranked: "已排名",
  not_ranked: "未入池",
  completed: "已完成",
  skipped: "已跳过",
  failed: "失败",
  success: "成功",
  at_down_limit: "处于跌停",
  at_up_limit: "处于涨停",
  excluded_financial_industry: "排除金融行业",
  insufficient_listing_history: "上市时间不足",
  low_liquidity: "流动性不足",
  special_treatment_or_delisting: "ST 或退市风险",
  data_update: "数据更新",
  market_analysis: "市场分析",
  industry_analysis: "行业分析",
  factor_analysis: "因子分析",
  daily_observation: "每日观察",
  dashboard: "界面生成",
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `请求失败 (${response.status})`);
  return value;
}

function toast(message, error = false) {
  const node = $("toast");
  node.textContent = message;
  node.className = `toast visible${error ? " error" : ""}`;
  window.clearTimeout(node._timer);
  node._timer = window.setTimeout(() => node.className = "toast", 3200);
}

function marketAdvice(market) {
  const marketState = market?.state;
  if (marketState === "high_risk") return "多数股票趋势偏弱。实际操作应先控制风险，避免仅因个股排名靠前就激进追涨。";
  if (marketState === "defensive") return "环境偏防守。优先关注低波动、现金流稳健方向，并降低试错频率。";
  if (marketState === "balanced") return "环境相对均衡。可研究结构性机会，但仍需结合行业扩散度和个股风险。";
  if (marketState === "attack") return "市场参与度较好。候选信号可获得更高关注，但仍不替代公告核查与仓位管理。";
  return "等待生成市场报告。";
}

function stateClass(value) {
  if (["trend_strengthening", "strong_continuation"].includes(value)) return "state-positive";
  if (["high_level_divergence", "trend_weakening"].includes(value)) return "state-warning";
  return "state-neutral";
}

function renderMarket(market) {
  const breadth = market?.breadth || {};
  const risk = market?.risk || {};
  $("marketState").textContent = labels[market?.state] || text(market?.state);
  $("temperature").textContent = num(market?.temperature, 1);
  $("temperatureBar").style.width = `${Math.max(0, Math.min(100, Number(market?.temperature || 0)))}%`;
  $("confidencePill").textContent = `置信度 ${text(market?.data_confidence)}`;
  $("marketAdvice").textContent = marketAdvice(market);
  $("historyRange").textContent = market?.history_start_date
    ? `${market.history_start_date} → ${market.history_end_date}` : "—";
  $("eligibleStocks").textContent = text(breadth.eligible_stocks);
  const values = [
    ["above20", "above20Bar", breadth.above_ma_20],
    ["above60", "above60Bar", breadth.above_ma_60],
    ["advanceRatio", "advanceBar", breadth.advance_ratio],
    ["newLow", "newLowBar", breadth.new_low_60_ratio],
  ];
  values.forEach(([label, bar, value]) => {
    $(label).textContent = pct(value);
    $(bar).style.width = `${Math.max(0, Math.min(100, Number(value || 0) * 100))}%`;
  });
  $("trendScore").textContent = num(market?.trend_score);
  $("breadthScore").textContent = num(breadth.score);
  $("healthScore").textContent = num(risk.health_score);
  $("riskCard").dataset.state = market?.state || "";
}

function renderIndustries(industry) {
  $("industryConfidence").textContent = `置信度 ${text(industry?.data_confidence)}`;
  const rows = (industry?.industries || []).slice(0, 12).map((item) => `
    <tr>
      <td class="rank-number">${escapeHtml(item.rank)}</td>
      <td><strong>${escapeHtml(item.name)}</strong><br><small>${escapeHtml(item.stock_count)} 只股票</small></td>
      <td><span class="state-tag ${stateClass(item.state)}">${escapeHtml(labels[item.state] || item.state)}</span></td>
      <td class="number">${num(item.score)}</td>
      <td class="number">${pct(item.relative_return_20d)}</td>
      <td class="number">${pct(item.above_ma_60)}</td>
    </tr>`).join("");
  $("industryRows").innerHTML = rows || `<tr><td colspan="6" class="empty-inline">暂无行业报告</td></tr>`;
}

function candidateRows() {
  const query = $("candidateSearch").value.trim().toLowerCase();
  const limit = Number($("candidateLimit").value);
  return state.candidates
    .filter((item) => [item.ts_code, item.name, item.industry].some(
      (value) => String(value || "").toLowerCase().includes(query)
    ))
    .slice(0, limit);
}

function renderCandidates() {
  const rows = candidateRows();
  $("candidateRows").innerHTML = rows.map((item) => `
    <tr data-code="${escapeHtml(item.ts_code)}">
      <td class="rank-number">${escapeHtml(item.rank)}</td>
      <td class="stock-cell"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.ts_code)}</small></td>
      <td>${escapeHtml(item.industry)}</td>
      <td class="number score-cell">${num(item.score)}</td>
      <td class="number">${num(item.quality_score)}</td>
      <td class="number">${num(item.value_score)}</td>
      <td class="number">${num(item.momentum_score)}</td>
      <td class="number">${num(item.low_risk_score)}</td>
    </tr>`).join("") || `<tr><td colspan="8" class="empty-inline">没有匹配的候选股票</td></tr>`;
  document.querySelectorAll("#candidateRows tr[data-code]").forEach((row) => {
    row.addEventListener("click", () => showCandidate(row.dataset.code));
  });
  $("candidateCount").textContent = text(state.candidates.length);
}

function showCandidate(code) {
  const item = state.candidates.find((value) => value.ts_code === code);
  if (!item) return;
  document.querySelectorAll("#candidateRows tr").forEach(
    (row) => row.classList.toggle("selected", row.dataset.code === code)
  );
  const factors = [
    ["质量", item.quality_score], ["价值", item.value_score],
    ["动量", item.momentum_score], ["低风险", item.low_risk_score],
  ];
  $("candidateDetail").innerHTML = `
    <span class="detail-rank">全市场排名 #${escapeHtml(item.rank)}</span>
    <h3>${escapeHtml(item.name)} <small>${escapeHtml(item.ts_code)}</small></h3>
    <p>${escapeHtml(item.industry)} · 收盘价 ${num(item.close, 2)}</p>
    <div class="factor-bars">${factors.map(([label, value]) => `
      <div class="factor-bar" data-score="${Number(value || 0)}"><span>${label}</span><div><i></i></div><strong>${num(value, 0)}</strong></div>
    `).join("")}</div>
    <ul class="reason-list">${(item.reasons || []).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>
    <p>财务数据：${escapeHtml(item.financial_period)}，公告日 ${escapeHtml(item.financial_ann_date)}</p>`;
  document.querySelectorAll("#candidateDetail .factor-bar").forEach((bar) => {
    bar.querySelector("i").style.width = `${bar.dataset.score}%`;
  });
}

function renderFilterStats(factors) {
  const stats = factors?.exclusion_counts || factors?.filter_stats || factors?.exclusions || {};
  $("filterStats").innerHTML = Object.entries(stats).map(
    ([key, value]) => `<span>${escapeHtml(labels[key] || key)}：${escapeHtml(value)}</span>`
  ).join("");
}

function renderWatchlist(observation) {
  const entries = observation?.watchlist || observation?.watchlist_entries || [];
  if (!entries.length) {
    $("watchlistResults").innerHTML = `<div class="empty-inline">尚无自选股结果。保存代码后，使用已有数据重算即可生成。</div>`;
    return;
  }
  $("watchlistResults").innerHTML = entries.map((item) => `
    <div class="watch-row">
      <div><strong>${escapeHtml(item.name || item.ts_code)}</strong><br><small>${escapeHtml(item.ts_code)}</small></div>
      <span class="state-tag ${item.status === "candidate" ? "state-positive" : "state-neutral"}">${escapeHtml(labels[item.status] || item.status)}</span>
      <strong class="watch-rank">${item.rank == null ? "—" : `#${item.rank}`}</strong>
    </div>`).join("");
}

function renderPipeline(pipeline, quality) {
  $("pipelineStatus").textContent = labels[pipeline?.status] || text(pipeline?.status, "无记录");
  $("pipelineSteps").innerHTML = (pipeline?.steps || []).map((step) => `
    <article class="step-item">
      <strong>${escapeHtml(labels[step.name] || step.name)}</strong>
      <span class="step-state step-${escapeHtml(step.status)}">${escapeHtml(labels[step.status] || step.status)}</span>
      <p>${escapeHtml(step.message)}</p>
    </article>`).join("") || `<div class="empty-inline">暂无运行记录</div>`;
  if (quality) {
    const issueCount = (quality.datasets || []).reduce(
      (total, item) => total + (item.issues || []).length, 0
    );
    $("qualitySummary").textContent = quality.passed
      ? `数据质量检查通过，共 ${quality.datasets?.length || 0} 个数据集，${issueCount} 个提示。`
      : `数据质量检查未通过，共发现 ${issueCount} 个问题。`;
  } else {
    $("qualitySummary").textContent = "该日期暂无数据质量报告。";
  }
}

function renderOverview(value) {
  state.overview = value;
  state.candidates = value.factors?.candidates || [];
  const available = Boolean(value.market || value.industry || value.factors || value.pipeline);
  $("emptyState").classList.toggle("hidden", available);
  $("dashboardContent").classList.toggle("hidden", !available);
  if (!available) return;
  renderMarket(value.market);
  renderIndustries(value.industry);
  renderCandidates();
  renderFilterStats(value.factors);
  renderWatchlist(value.observation);
  renderPipeline(value.pipeline, value.quality);
}

function selectedProtocol() {
  const protocolId = $("protocolSelect").value;
  return (state.backtestMeta?.protocols || []).find((item) => item.protocol_id === protocolId);
}

function renderProtocol() {
  const protocol = selectedProtocol();
  const existing = Boolean(protocol);
  const fields = [
    "protocolId", "protocolTitle", "protocolHypothesis",
    "developmentStart", "developmentEnd", "validationStart",
    "validationEnd", "holdoutStart", "holdoutEnd", "allowedTrials",
  ];
  fields.forEach((id) => $(id).disabled = existing);
  $("protocolSummary").classList.toggle("hidden", !existing);
  if (existing) {
    $("protocolId").value = protocol.protocol_id;
    $("protocolTitle").value = protocol.title;
    $("protocolHypothesis").value = protocol.hypothesis;
    ["development", "validation", "holdout"].forEach((name) => {
      const partition = protocol.partitions[name];
      $(`${name}Start`).value = partition?.start_date || "";
      $(`${name}End`).value = partition?.end_date || "";
    });
    $("allowedTrials").value = protocol.allowed_trials;
    $("protocolSummary").innerHTML = `
      <h3>${escapeHtml(protocol.title)}</h3>
      <p>${escapeHtml(protocol.hypothesis)}</p>
      <p>状态：<strong>${protocol.status === "frozen" ? "已冻结，可运行正式回测" : "草稿，历史信号尚未全部固定"}</strong></p>
      <p>验证集已运行 ${escapeHtml(protocol.trial_counts.validation || 0)} / ${escapeHtml(protocol.allowed_trials)} 次 ·
      封存集${protocol.holdout_revealed ? "已经揭晓" : "仍未揭晓"}</p>`;
  }
  const frozen = protocol?.status === "frozen";
  $("prepareBacktest").disabled = frozen;
  $("prepareBacktest").textContent = existing ? (frozen ? "方案已经准备完成" : "继续准备并冻结") : "创建方案并准备历史信号";
  $("backtestMode").textContent = protocol
    ? `${protocol.protocol_id} · ${frozen ? "正式验证" : "草稿"}`
    : "创建新方案";
  ["development", "validation", "holdout"].forEach((name) => {
    const partition = protocol?.partitions[name];
    $(`${name}Range`).textContent = partition
      ? `${partition.start_date} → ${partition.end_date}` : "—";
    const button = document.querySelector(`.partition-button[data-partition="${name}"]`);
    button.disabled = !frozen;
    button.classList.remove("selected");
  });
  state.selectedPartition = null;
  $("runBacktest").disabled = true;
  $("holdoutConfirmRow").classList.add("hidden");
  $("holdoutConfirm").checked = false;
}

async function loadBacktestMeta(selectProtocolId = null) {
  const meta = await api("/api/backtest/meta");
  state.backtestMeta = meta;
  const select = $("protocolSelect");
  const previous = selectProtocolId || select.value;
  select.innerHTML = `<option value="">创建新方案</option>` + meta.protocols.map(
    (item) => `<option value="${escapeHtml(item.protocol_id)}">${escapeHtml(item.title)} · ${item.status === "frozen" ? "已冻结" : "草稿"}</option>`
  ).join("");
  if (previous && meta.protocols.some((item) => item.protocol_id === previous)) select.value = previous;
  const completed = meta.results.filter((item) => item.has_result);
  $("backtestResultSelect").innerHTML = completed.length
    ? `<option value="">选择已有结果</option>` + completed.map((item) =>
      `<option value="${escapeHtml(item.experiment_id)}">${escapeHtml(item.protocol_id || "探索")} · ${escapeHtml(item.partition)} · ${escapeHtml(item.start_date)} 至 ${escapeHtml(item.end_date)}</option>`
    ).join("")
    : `<option value="">暂无结果</option>`;
  renderProtocol();
}

function backtestPayload() {
  return {
    protocol_id: $("protocolId").value.trim(),
    title: $("protocolTitle").value.trim(),
    hypothesis: $("protocolHypothesis").value.trim(),
    development_start: $("developmentStart").value,
    development_end: $("developmentEnd").value,
    validation_start: $("validationStart").value,
    validation_end: $("validationEnd").value,
    holdout_start: $("holdoutStart").value,
    holdout_end: $("holdoutEnd").value,
    allowed_trials: Number($("allowedTrials").value),
  };
}

async function prepareBacktest() {
  const payload = backtestPayload();
  if (!payload.protocol_id || !payload.title || !payload.hypothesis) {
    return toast("请完整填写方案 ID、名称和研究假设。", true);
  }
  try {
    await api("/api/backtest/prepare", { method: "POST", body: JSON.stringify(payload) });
    setBacktestButtons(false);
    updateBacktestTask({ state: "running", action: "prepare", protocol_id: payload.protocol_id, output: "" });
    pollBacktestTask();
  } catch (error) {
    toast(error.message, true);
  }
}

function selectPartition(partition) {
  state.selectedPartition = partition;
  document.querySelectorAll(".partition-button").forEach(
    (button) => button.classList.toggle("selected", button.dataset.partition === partition)
  );
  $("holdoutConfirmRow").classList.toggle("hidden", partition !== "holdout");
  $("holdoutConfirm").checked = false;
  $("runBacktest").disabled = partition === "holdout";
}

async function runBacktest() {
  const protocol = selectedProtocol();
  if (!protocol || !state.selectedPartition) return toast("请先选择已冻结方案和回测区间。", true);
  const holdout = state.selectedPartition === "holdout";
  if (holdout && !$("holdoutConfirm").checked) return toast("揭晓封存集前必须勾选确认。", true);
  try {
    await api("/api/backtest/run", {
      method: "POST",
      body: JSON.stringify({
        protocol_id: protocol.protocol_id,
        partition: state.selectedPartition,
        confirm_holdout: holdout && $("holdoutConfirm").checked,
      }),
    });
    setBacktestButtons(false);
    updateBacktestTask({
      state: "running", action: "run", protocol_id: protocol.protocol_id,
      partition: state.selectedPartition, output: "",
    });
    pollBacktestTask();
  } catch (error) {
    toast(error.message, true);
  }
}

function setBacktestButtons(enabled) {
  $("prepareBacktest").disabled = !enabled || selectedProtocol()?.status === "frozen";
  document.querySelectorAll(".partition-button").forEach(
    (button) => button.disabled = !enabled || selectedProtocol()?.status !== "frozen"
  );
  $("runBacktest").disabled = !enabled || !state.selectedPartition ||
    (state.selectedPartition === "holdout" && !$("holdoutConfirm").checked);
}

function updateBacktestTask(task) {
  const banner = $("backtestTask");
  banner.classList.remove("hidden", "success", "failure");
  $("backtestLog").textContent = task.output || "任务已启动。准备历史信号可能需要几分钟，请保持页面打开。";
  if (task.state === "running") {
    $("backtestTaskTitle").textContent = task.action === "prepare" ? "正在准备防作弊回测方案" : "正在运行策略回测";
    $("backtestTaskDetail").textContent = `${task.protocol_id || ""}${task.partition ? ` · ${task.partition}` : ""}`;
    return;
  }
  const success = task.state === "completed";
  banner.classList.add(success ? "success" : "failure");
  $("backtestTaskTitle").textContent = success ? "回测任务完成" : "回测任务失败";
  $("backtestTaskDetail").textContent = task.finished_at || "";
}

async function pollBacktestTask() {
  window.clearTimeout(state.backtestTimer);
  try {
    const task = await api("/api/backtest/task");
    if (task.state === "idle") return;
    updateBacktestTask(task);
    if (task.state === "running") {
      setBacktestButtons(false);
      state.backtestTimer = window.setTimeout(pollBacktestTask, 1500);
      return;
    }
    setBacktestButtons(true);
    await loadBacktestMeta(task.protocol_id);
    if (task.result_id && task.state === "completed") {
      $("backtestResultSelect").value = task.result_id;
      await loadBacktestResult(task.result_id);
    }
    toast(task.state === "completed" ? "回测任务已完成。" : "回测失败，请查看日志。", task.state !== "completed");
  } catch (error) {
    setBacktestButtons(true);
    toast(error.message, true);
  }
}

function renderEquityChart(curve) {
  const svg = $("equityChart");
  if (!curve.length) {
    svg.innerHTML = `<text x="450" y="140" text-anchor="middle" class="chart-label">没有权益曲线数据</text>`;
    return;
  }
  const portfolioBase = Number(curve[0].equity) || 1;
  const benchmarkBase = Number(curve[0].benchmark_equity) || 1;
  const rows = curve.map((item) => ({
    date: item.trade_date,
    portfolio: Number(item.equity) / portfolioBase,
    benchmark: Number(item.benchmark_equity) / benchmarkBase,
  }));
  const values = rows.flatMap((item) => [item.portfolio, item.benchmark]);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = Math.max(max - min, .01);
  const point = (value, index) => `${40 + index * 830 / Math.max(rows.length - 1, 1)},${20 + (max - value) * 220 / span}`;
  const portfolio = rows.map((item, index) => point(item.portfolio, index)).join(" ");
  const benchmark = rows.map((item, index) => point(item.benchmark, index)).join(" ");
  svg.innerHTML = `
    <line x1="40" y1="20" x2="40" y2="240" class="chart-grid"/>
    <line x1="40" y1="240" x2="870" y2="240" class="chart-grid"/>
    <line x1="40" y1="130" x2="870" y2="130" class="chart-grid"/>
    <polyline points="${portfolio}" class="chart-portfolio"/>
    <polyline points="${benchmark}" class="chart-benchmark"/>
    <text x="40" y="263" class="chart-label">${escapeHtml(rows[0].date)}</text>
    <text x="870" y="263" text-anchor="end" class="chart-label">${escapeHtml(rows[rows.length - 1].date)}</text>
    <text x="34" y="24" text-anchor="end" class="chart-label">${max.toFixed(2)}</text>
    <text x="34" y="243" text-anchor="end" class="chart-label">${min.toFixed(2)}</text>`;
}

function renderPosition(positionIndex = null) {
  const positions = state.positions || [];
  if (!positions.length) {
    $("positionSummary").textContent = "该回测没有可展示的持仓明细。";
    $("positionRows").innerHTML = "";
    $("removedPositions").innerHTML = "";
    return;
  }
  const index = positionIndex == null
    ? positions.length - 1
    : Math.max(0, Math.min(positions.length - 1, Number(positionIndex)));
  const position = positions[index];
  $("positionDateSelect").value = String(index);
  const cost = Number(position.transaction_cost || 0) + Number(position.slippage_cost || 0);
  $("positionSummary").innerHTML = `
    <span>信号日：<strong>${escapeHtml(position.signal_date)}</strong></span>
    <span>执行：<strong>${escapeHtml(position.execution_start)}${position.execution_end !== position.execution_start ? ` → ${escapeHtml(position.execution_end)}` : ""}</strong></span>
    <span>换手：<strong>${pct(position.turnover, 1)}</strong></span>
    <span>费用：<strong>${num(cost, 0)}</strong></span>
    <span>受限买入/卖出：<strong>${escapeHtml(position.blocked_buys)}/${escapeHtml(position.blocked_sells)}</strong></span>`;
  $("positionRows").innerHTML = (position.holdings || []).map((item) => `
    <tr>
      <td><span class="change-tag change-${item.change === "added" ? "added" : "held"}">${item.change === "added" ? "新买入" : "继续持有"}</span></td>
      <td><strong>${escapeHtml(item.ts_code)}</strong></td>
      <td>${escapeHtml(item.name || "名称缺失")}</td>
      <td>${escapeHtml(item.industry || "行业缺失")}</td>
    </tr>`).join("") || `<tr><td colspan="4" class="empty-inline">该期没有持仓</td></tr>`;
  const removed = position.removed || [];
  $("removedPositions").innerHTML = removed.length
    ? `<strong>相对上期卖出：</strong><br>${removed.map(
      (item) => `<span class="removed-chip">${escapeHtml(item.name || item.ts_code)} · ${escapeHtml(item.ts_code)}</span>`
    ).join("")}`
    : `<strong>相对上期卖出：</strong> 无`;
}

function renderPositionHistory(positions) {
  state.positions = positions || [];
  $("positionDateSelect").innerHTML = state.positions.length
    ? state.positions.map((item, index) =>
      `<option value="${index}">${escapeHtml(item.signal_date)} · 持仓 ${escapeHtml(item.holdings?.length || 0)} 只</option>`
    ).join("")
    : `<option value="">暂无持仓明细</option>`;
  renderPosition();
}

async function loadBacktestResult(experimentId) {
  if (!experimentId) {
    $("backtestEmpty").classList.remove("hidden");
    $("backtestResultContent").classList.add("hidden");
    return;
  }
  try {
    const result = await api(`/api/backtest/result?id=${encodeURIComponent(experimentId)}`);
    const summary = result.summary;
    $("backtestEmpty").classList.add("hidden");
    $("backtestResultContent").classList.remove("hidden");
    $("btTotalReturn").textContent = pct(summary.portfolio?.total_return, 2);
    $("btBenchmarkReturn").textContent = `基准 ${pct(summary.benchmark?.total_return, 2)}`;
    $("btAnnualReturn").textContent = pct(summary.portfolio?.annualized_return, 2);
    $("btSharpe").textContent = `Sharpe ${num(summary.portfolio?.sharpe_ratio, 2)}`;
    $("btDrawdown").textContent = pct(summary.portfolio?.max_drawdown, 2);
    $("btBenchmarkDrawdown").textContent = `基准 ${pct(summary.benchmark?.max_drawdown, 2)}`;
    $("btRebalances").textContent = text(summary.rebalance_count);
    $("btCosts").textContent = `累计成本 ${num(summary.total_cost, 0)}`;
    renderEquityChart(result.curve || []);
    renderPositionHistory(result.positions || []);
    $("backtestAudit").innerHTML = `
      <strong>${summary.protocol_id ? "正式验证回测" : "探索性回测"}</strong> ·
      ${escapeHtml(summary.start_date)} 至 ${escapeHtml(summary.end_date)} ·
      时间泄漏审计：${summary.leakage_audit_passed ? "通过" : "未通过"} ·
      信号版本 ${Object.keys(summary.signal_versions || {}).length} 个 ·
      受限买入/卖出 ${escapeHtml(summary.blocked_buy_orders)}/${escapeHtml(summary.blocked_sell_orders)}。
      <br>回测尚未模拟整手、成交容量和盘中价格路径，结果不构成投资建议。`;
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadOverview(dateValue) {
  if (!dateValue) return;
  try {
    renderOverview(await api(`/api/overview?date=${encodeURIComponent(dateValue)}`));
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadMeta(selectLatest = true) {
  const meta = await api("/api/meta");
  state.meta = meta;
  $("providerName").textContent = meta.provider;
  $("tokenState").textContent = meta.token_configured ? "已配置" : "未配置";
  $("tokenState").className = `dot-state ${meta.token_configured ? "ok" : "bad"}`;
  $("apiState").textContent = meta.api_url_configured ? "自定义" : "官方";
  $("watchlistInput").value = (meta.watchlist || []).join("\n");
  const history = $("historyDate");
  history.innerHTML = meta.dates.length
    ? meta.dates.map((date) => `<option value="${date}">${date} 已有报告</option>`).join("")
    : `<option value="">暂无历史报告</option>`;
  if (selectLatest && meta.latest_date) {
    history.value = meta.latest_date;
    $("tradeDate").value = meta.latest_date;
    await loadOverview(meta.latest_date);
  }
}

async function runPipeline(mode) {
  const dateValue = $("tradeDate").value;
  if (!dateValue) return toast("请先选择分析日期。", true);
  if (mode === "update" && !state.meta?.token_configured) {
    return toast("TUSHARE_TOKEN 尚未配置，不能联网更新。", true);
  }
  try {
    await api("/api/run", {
      method: "POST",
      body: JSON.stringify({ date: dateValue, mode }),
    });
    setRunButtons(false);
    updateTask({ state: "running", date: dateValue, mode, output: "" });
    pollTask();
  } catch (error) {
    toast(error.message, true);
  }
}

function setRunButtons(enabled) {
  $("runExisting").disabled = !enabled;
  $("runUpdate").disabled = !enabled;
}

function updateTask(task) {
  const banner = $("taskBanner");
  banner.classList.remove("hidden", "success", "failure");
  $("taskLog").textContent = task.output || "任务已启动，等待后台输出...";
  if (task.state === "running") {
    $("taskTitle").textContent = task.mode === "existing" ? "正在使用现有数据重算" : "正在更新数据并分析";
    $("taskDetail").textContent = `${task.date} · 页面可以保持打开，任务将在后台继续`;
    return;
  }
  const success = task.state === "completed";
  banner.classList.add(success ? "success" : "failure");
  $("taskTitle").textContent = success ? "研究报告生成完成" : "任务执行失败";
  $("taskDetail").textContent = `${task.date} · ${task.finished_at || ""}`;
}

async function pollTask() {
  window.clearTimeout(state.taskTimer);
  try {
    const task = await api("/api/task");
    if (task.state === "idle") return;
    updateTask(task);
    if (task.state === "running") {
      setRunButtons(false);
      state.taskTimer = window.setTimeout(pollTask, 1500);
      return;
    }
    setRunButtons(true);
    await loadMeta(false);
    $("historyDate").value = task.date;
    $("tradeDate").value = task.date;
    await loadOverview(task.date);
    toast(task.state === "completed" ? "报告已更新。" : "任务失败，请查看日志。", task.state !== "completed");
  } catch (error) {
    setRunButtons(true);
    toast(error.message, true);
  }
}

async function saveWatchlist() {
  const symbols = $("watchlistInput").value.split(/\r?\n|,|，/).map((value) => value.trim()).filter(Boolean);
  try {
    const result = await api("/api/watchlist", {
      method: "PUT",
      body: JSON.stringify({ symbols }),
    });
    $("watchlistInput").value = result.watchlist.join("\n");
    $("watchlistMessage").textContent = `已保存 ${result.watchlist.length} 只`;
    toast("自选股已保存。使用已有数据重算后即可看到状态。");
    await loadMeta(false);
  } catch (error) {
    $("watchlistMessage").textContent = error.message;
    toast(error.message, true);
  }
}

function bindEvents() {
  $("historyDate").addEventListener("change", (event) => {
    if (!event.target.value) return;
    $("tradeDate").value = event.target.value;
    loadOverview(event.target.value);
  });
  $("tradeDate").addEventListener("change", (event) => loadOverview(event.target.value));
  $("runUpdate").addEventListener("click", () => runPipeline("update"));
  $("runExisting").addEventListener("click", () => runPipeline("existing"));
  $("saveWatchlist").addEventListener("click", saveWatchlist);
  $("candidateSearch").addEventListener("input", renderCandidates);
  $("candidateLimit").addEventListener("change", renderCandidates);
  $("toggleLog").addEventListener("click", () => $("taskLog").classList.toggle("hidden"));
  $("protocolSelect").addEventListener("change", renderProtocol);
  $("prepareBacktest").addEventListener("click", prepareBacktest);
  document.querySelectorAll(".partition-button").forEach(
    (button) => button.addEventListener("click", () => selectPartition(button.dataset.partition))
  );
  $("holdoutConfirm").addEventListener("change", () => setBacktestButtons(true));
  $("runBacktest").addEventListener("click", runBacktest);
  $("toggleBacktestLog").addEventListener("click", () => $("backtestLog").classList.toggle("hidden"));
  $("backtestResultSelect").addEventListener("change", (event) => loadBacktestResult(event.target.value));
  $("positionDateSelect").addEventListener("change", (event) => renderPosition(event.target.value));
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      $(button.dataset.scroll)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
}

async function init() {
  bindEvents();
  try {
    await loadMeta(true);
    await loadBacktestMeta();
    const task = await api("/api/task");
    if (task.state !== "idle") {
      updateTask(task);
      if (task.state === "running") {
        setRunButtons(false);
        pollTask();
      }
    }
    const backtestTask = await api("/api/backtest/task");
    if (backtestTask.state !== "idle") {
      updateBacktestTask(backtestTask);
      if (backtestTask.state === "running") {
        setBacktestButtons(false);
        pollBacktestTask();
      }
    }
  } catch (error) {
    toast(`界面初始化失败：${error.message}`, true);
  }
}

init();
