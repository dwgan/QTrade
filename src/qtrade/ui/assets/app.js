const state = {
  meta: null,
  overview: null,
  candidates: [],
  taskTimer: null,
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
    const task = await api("/api/task");
    if (task.state !== "idle") {
      updateTask(task);
      if (task.state === "running") {
        setRunButtons(false);
        pollTask();
      }
    }
  } catch (error) {
    toast(`界面初始化失败：${error.message}`, true);
  }
}

init();
