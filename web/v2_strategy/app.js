"use strict";
const $ = id => document.getElementById(id);
const workspace = typeof window !== "undefined" ? window.Atlas : null;
const TABS = ["overview", "trades", "signals", "risk", "history"];
const PAGE_SIZE = 20;
let accounts = [], generation = 0, revision = 0, policy = null, analytics = null, pending = null, publishing = false;
let tradePage = 1, chartPoints = [], selected = {environment: "SANDBOX", account: "", days: "14"};
let executionRegistry = null, currentTab = "overview", dirty = false, accountsReady = false;
let reviewSequence = 0;
let shadowSequence = 0;
const inputs = new Map(), fieldErrors = new Map();
const FIELDS = {
  "capital.pool_fraction": ["总保证金使用上限", "%", "总预算最多使用 70%，定仓资金池会自动同步。"],
  "sizing.max_margin_fraction": ["单笔保证金上限", "%", "单笔最多占风险预算基数的比例，实际仓位仍受止损和其他限制。"],
  "sizing.risk_fraction": ["单笔止损风险预算", "%", "一笔交易按计划止损估算的风险；滑点可能使实际亏损更大。"],
  "portfolio_risk.max_fraction": ["组合止损风险预算", "%", "所有持仓计划止损风险的合计上限。"],
  "sizing.min_allocation": ["剩余资金池最低分配", "%", "评分定仓使用的分配下界，不是强制开仓金额。", true],
  "sizing.max_allocation": ["剩余资金池最高分配", "%", "评分定仓使用的分配上界，仍受单笔和风险预算约束。", true],
  "capital.margin_buffer": ["保证金预占缓冲", "倍", "例如 1.1 倍，按预计保证金的 110% 预占。", true],
  "sizing.cost_buffer_fraction": ["定仓成本缓冲", "%", "为费用和执行成本预留的估算比例。", true]
};
const LIMITS = {SCORE_ALLOCATION_AFTER_VOLATILITY: "评分与波动率限制", AVAILABLE_MARGIN: "可用保证金不足", SINGLE_MARGIN_CAP: "单笔保证金上限", STOP_RISK_CAP: "止损风险预算", RISK_SIZED_QUANTITY: "风险定仓数量", VENUE_MAX_QUANTITY: "交易所数量上限", VENUE_MAX_NOTIONAL: "交易所名义金额上限"};
const REASONS = {
  score_below_minimum: ["综合评分未达标", "价格、趋势等条件的综合评分未达到入场门槛。"],
  score_unreachable: ["基础条件不足", "即使采用最高情绪加分，也无法达到入场评分。"],
  regime_conflict: ["市场状态不匹配", "信号方向与当前市场状态不一致。"],
  overextended: ["价格偏离过大", "当前价格距离参考均线过远，追价检查未通过。"],
  strength_below_minimum: ["信号强度不足", "信号强度未达到策略门槛。"],
  unsupported_event: ["不适用当前策略", "该信号类型不由这个方向性策略处理。"],
  entry_mode_unconfirmed: ["入场模式未确认", "当前价格与趋势尚未形成可确认的入场模式。"],
  atr_exceeded: ["波动率超限", "波动幅度超过当前策略允许范围。"],
  SIGNAL_EXPIRED: ["信号已过期", "信号超过有效期，不再追单。"],
  EXPIRED: ["信号已过期", "信号超过有效期，不再追单。"],
  EXISTING_SYMBOL_POSITION: ["已有同币种仓位", "同一账户已存在该币种敞口。"],
  ACCOUNT_MARGIN_BUDGET_EXCEEDED: ["保证金预算不足", "现有持仓和预占金额已达到预算上限。"],
  CAPITAL_DRAWDOWN_HALT: ["回撤保护生效", "账户回撤达到暂停新开仓的条件。"],
  SYMBOL_NOT_TRADING: ["合约不可交易", "交易所未提供符合条件的可交易合约。"],
  TV_SYMBOL_OUTSIDE_MARKET_UNIVERSE: ["币种未进入行情范围", "TradingView 币种不在当前可验证的行情范围内。"],
  TV_BEFORE_ENABLE_TIME: ["接入前的旧信号", "该信号早于账户启用 TradingView 的时间。"],
  QUANT_ACCOUNT_DISABLED: ["量化候选尚未启用", "当前账户没有启用新量化候选的准入权限。"]
};
const ERRORS = {
  POLICY_VERSION_CONFLICT: "配置已被其他操作更新。请刷新后重新预览，避免覆盖新版本。",
  POLICY_PREVIEW_MISMATCH: "提交与预览不一致，请重新预览。", POLICY_PREVIEW_EXPIRED: "预览已过期，请重新预览。",
  POLICY_NO_CHANGE: "没有实际变化，请先修改参数。", POLICY_REASON_REQUIRED: "请填写变更原因。",
  POLICY_RESERVE_30_PERCENT_REQUIRED: "总保证金预算不能超过 70%。", POLICY_SINGLE_MARGIN_EXCEEDS_POOL: "单笔保证金上限不能大于总预算。",
  POLICY_SINGLE_RISK_EXCEEDS_PORTFOLIO: "单笔风险不能大于组合风险。", POLICY_SANDBOX_ONLY: "生产环境仅可查看。",
  POLICY_REQUEST_CONFLICT: "重复请求内容不同，请刷新核对配置版本。"
};
function text(tag, value, className) { const el = document.createElement(tag); el.textContent = value ?? "—"; if (className) el.className = className; return el; }
function number(value, places = 2) {
  if (value === null || value === undefined || value === "") return "—";
  const parts = String(value).split("/"), n = Number(parts[0]) / (parts.length === 2 ? Number(parts[1]) : 1);
  return Number.isFinite(n) ? n.toLocaleString("zh-CN", {maximumFractionDigits: places}) : "—";
}
function tone(value) { return value !== null && value !== undefined && Number(value) < 0 ? "negative" : Number(value) > 0 ? "positive" : ""; }
function source(value) { return ({tv_bridge: "TradingView", s3: "行情扫描", quant: "量化策略"})[value] || value || "未记录"; }
function duration(value) { if (value === null || value === undefined) return "未记录"; const n = Number(value); return n >= 1440 ? `${number(n / 1440, 1)} 天` : n >= 60 ? `${number(n / 60, 1)} 小时` : `${number(n, 1)} 分钟`; }
function date(value) { const d = new Date(value); return !value || Number.isNaN(d.getTime()) ? "未记录" : d.toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}); }
function accountName() { return accounts.find(a => a.registry_id === $("account").value)?.alias || "所选账户"; }
function empty(target, title, note) { const box = text("div", "", "empty-state"); box.append(text("span", "◇", "empty-symbol"), text("strong", title), text("p", note)); $(target).replaceChildren(box); }
function simpleTable(target, headers, rows) {
  if (!rows.length) { empty(target, "暂无记录", "所选账户与统计范围内没有可展示的数据。"); return; }
  const table = document.createElement("table"), head = document.createElement("thead"), tr = document.createElement("tr"), body = document.createElement("tbody");
  headers.forEach(h => tr.append(text("th", h))); head.append(tr); table.append(head);
  rows.forEach(values => { const row = document.createElement("tr"); values.forEach(value => { const cell = document.createElement("td"); typeof value === "object" && value !== null ? cell.append(value) : cell.append(text("span", value)); row.append(cell); }); body.append(row); });
  table.append(body); $(target).replaceChildren(table);
}
async function api(path, body) {
  const response = await fetch(path, {method: body ? "POST" : "GET", credentials: "same-origin", cache: "no-store", headers: body ? {"Content-Type": "application/json", "X-V2-Action": "strategy-management"} : {}, ...(body ? {body: JSON.stringify(body)} : {})});
  let data; try { data = await response.json(); } catch (_) { throw new Error(response.status === 401 ? "登录已失效，请重新登录后刷新。" : "服务未返回有效数据，请稍后重试。"); }
  if (!response.ok) throw new Error(ERRORS[data.error] || "服务暂不可用或参数不被接受，请刷新核对。");
  return data;
}
function targetPath(registry, action) { return `/api/accounts/${encodeURIComponent(registry)}/strategy/${action}`; }

// String-only decimal shifting: displaying 5% must send exactly "0.05", never
// floating-point artifacts or accidental changes to untouched high-precision fields.
function decimalShift(value, shift = 0) {
  const raw = String(value).trim();
  if (raw.length > 80 || !/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(raw)) throw new Error("请输入数字，不要输入 %、负数或科学计数法。");
  const [whole, fraction = ""] = raw.split("."), digits = (whole || "0") + fraction, point = (whole || "0").length + shift;
  let result = point <= 0 ? "0." + "0".repeat(-point) + digits : point >= digits.length ? digits + "0".repeat(point - digits.length) : digits.slice(0, point) + "." + digits.slice(point);
  result = result.replace(/^0+(?=\d)/, "");
  if (result.includes(".")) result = result.replace(/0+$/, "").replace(/\.$/, "");
  return result || "0";
}
function displayValue(key, value) { return decimalShift(value, FIELDS[key]?.[1] === "%" ? 2 : 0); }
function rawValue(key, value) { return decimalShift(value, FIELDS[key]?.[1] === "%" ? -2 : 0); }
function unitValue(key, value) { try { return `${number(displayValue(key, value), 8)}${FIELDS[key]?.[1] || ""}`; } catch (_) { return String(value); } }
function draft() {
  const changes = {}, invalid = new Map();
  inputs.forEach((input, key) => {
    try {
      const raw = rawValue(key, input.value), field = policy.catalog.find(f => f.key === key);
      if (field && (Number(raw) < Number(field.minimum) || Number(raw) > Number(field.maximum))) throw new Error(`允许范围：${unitValue(key, field.minimum)}–${unitValue(key, field.maximum)}`);
      if (raw !== decimalShift(policy.values[key])) changes[key] = raw;
    } catch (e) { invalid.set(key, e.message); }
  });
  if (changes["capital.pool_fraction"] !== undefined && policy.values["sizing.pool_fraction"] !== undefined) changes["sizing.pool_fraction"] = changes["capital.pool_fraction"];
  const value = key => Number(changes[key] ?? policy.values[key]);
  if (value("sizing.max_margin_fraction") > value("capital.pool_fraction")) invalid.set("sizing.max_margin_fraction", "不能大于总保证金使用上限。");
  if (value("sizing.risk_fraction") > value("portfolio_risk.max_fraction")) invalid.set("sizing.risk_fraction", "不能大于组合止损风险预算。");
  if (value("sizing.min_allocation") > value("sizing.max_allocation")) invalid.set("sizing.min_allocation", "最低分配不能大于最高分配。");
  return {changes, invalid};
}
function updateDraft() {
  if (!policy) return;
  const result = draft();
  // Invalid legacy values are not user edits, especially on a read-only account.
  dirty = policy.editable && [...inputs].some(([key, input]) => {
    try { return rawValue(key, input.value) !== decimalShift(policy.values[key]); }
    catch (_) { return true; }
  });
  const locked = publishing || Boolean(pending?.body);
  inputs.forEach((input, key) => { input.disabled = !policy.editable || locked; input.setAttribute("aria-invalid", String(result.invalid.has(key))); const error = fieldErrors.get(key); error.textContent = result.invalid.get(key) || ""; error.hidden = !result.invalid.has(key); });
  $("reason").disabled = locked;
  $("history").disabled = locked;
  $("restore").disabled = locked || !policy.editable || !policy.history.some(h => !h.deleted && h.version < policy.version);
  $("dirty-dot").hidden = !dirty && !pending;
  $("discard").disabled = !dirty && !pending;
  $("preview").disabled = !policy.editable || !dirty || result.invalid.size > 0 || locked;
  $("draft-status").textContent = !policy.editable ? "只读查看，没有未保存的修改。" : result.invalid.size ? "请先修正标出的参数，再预览。" : dirty ? "有未发布修改，执行器仍使用当前已保存版本。" : "与当前已保存配置一致。";
  const poolInput = inputs.get("capital.pool_fraction");
  const pool = poolInput ? Number(poolInput.value) : Number(policy.values["capital.pool_fraction"]) * 100;
  $("pool-label").textContent = Number.isFinite(pool) ? `${number(pool)}%` : "—";
  $("pool-meter").value = Number.isFinite(pool) ? Math.min(100, Math.max(0, pool)) : 0;
  $("reserve-label").textContent = Number.isFinite(pool) && pool >= 0 && pool <= 70 ? `预算至少预留 ${number(100 - pool)}%。这是使用上限，不是实际占用。` : "总预算不得超过 70%，至少保留 30%。";
  const sizingPool = result.changes["sizing.pool_fraction"] ?? policy.values["sizing.pool_fraction"];
  const capitalPool = result.changes["capital.pool_fraction"] ?? policy.values["capital.pool_fraction"];
  $("pool-config-note").textContent = sizingPool !== undefined && Number(sizingPool) !== Number(capitalPool) ? `历史定仓资金池上限为 ${unitValue("capital.pool_fraction", sizingPool)}，与总上限不同。${policy.editable ? "修改总上限并发布时会同步两项。" : "当前仅展示，不修改任何参数。"}` : "";
}
function invalidate() { ++revision; pending = null; $("apply").disabled = true; $("publish-fields").hidden = true; $("preview-context").textContent = "先调整左侧参数，再预览影响。"; $("preview-expiry").textContent = ""; $("apply-status").textContent = ""; empty("preview-result", "预览尚未生成", "调整参数后点击“预览修改”，再决定是否发布。"); updateDraft(); }
function discard() { if (!policy || publishing || (pending?.body && !mayDiscard())) return; inputs.forEach((input, key) => { input.value = displayValue(key, policy.values[key]); }); $("reason").value = ""; invalidate(); }
function showTab(name, {focus = false, writeHash = true} = {}) {
  if (!TABS.includes(name)) name = "overview";
  currentTab = name;
  TABS.forEach(tab => { const active = tab === name; $("panel-" + tab).hidden = !active; $("tab-" + tab).setAttribute("aria-selected", String(active)); $("tab-" + tab).tabIndex = active ? 0 : -1; });
  if (writeHash && window.history) window.history.replaceState(null, "", "#" + name);
  if (focus) $("tab-" + name).focus();
}
function mayDiscard() { return (!dirty && !pending) || window.confirm(pending?.body ? "上次发布结果尚未核实。切换后请检查配置版本；仍要离开当前草稿吗？" : "有尚未发布的修改。丢弃修改并继续？"); }
function closeTrade() { if ($("trade-dialog").open) $("trade-dialog").close(); }
function clear() {
  reviewSequence++;
  shadowSequence++;
  $("shadow-load").disabled = false;
  $("shadow-status").textContent = "点击读取所选账户的观测结果。";
  ["shadow-rows", "shadow-notes", "shadow-coverage", "shadow-evaluation-status", "shadow-groups"].forEach(id => $(id).replaceChildren());
  $("review-load").disabled = false;
  $("review-status").textContent = "点击生成，查看所选账户的只读复盘。";
  ["review-variants", "review-trades", "review-notes", "review-risk-summary", "review-risk-groups"].forEach(id => $(id).replaceChildren());
  policy = null; analytics = null; inputs.clear(); fieldErrors.clear(); dirty = false; chartPoints = []; tradePage = 1; closeTrade(); invalidate();
  for (const id of ["fields", "advanced-fields", "summary", "groups", "trades", "limits", "receipts", "limitations", "history", "history-list", "equity-summary", "equity-chart", "equity-cash", "equity-notes", "equity-coverage", "trade-detail"]) $(id).replaceChildren();
  for (const id of ["full-policy", "apply-status", "receipt-coverage", "sizing-coverage", "updated-at", "trade-page-info", "trade-scope", "reserve-label", "pool-config-note", "edit-notice"]) $(id).textContent = "";
  $("reason").disabled = false; $("history").disabled = false;
  $("reason").value = ""; $("trade-search").value = ""; $("trade-result").value = ""; $("trade-sort").value = "recent";
  $("trade-source").replaceChildren(new Option("全部来源", ""));
  $("equity-total").textContent = "—"; $("equity-badge").textContent = "等待采样"; $("equity-badge").className = "badge subtle";
  $("chart-point").disabled = true; $("chart-readout").textContent = "等待净值观测数据";
  $("policy-status").textContent = "等待配置…"; $("risk-insight").textContent = "等待配置"; $("signal-insight").textContent = "等待数据";
  $("pool-label").textContent = "—"; $("pool-meter").value = 0; $("trade-count").textContent = "—"; $("sample-badge").textContent = "等待数据";
  $("draft-status").textContent = "等待配置"; $("dirty-dot").hidden = true;
  ["preview", "restore", "discard", "trade-prev", "trade-next"].forEach(id => $(id).disabled = true);
}
function renderPolicy(data) {
  policy = data;
  $("policy-status").textContent = `已保存版本 v${data.version} · ${data.editable ? "可修改测试账户参数" : "当前账户只读"}`;
  $("edit-notice").textContent = data.editable ? "仅修改所选 Testnet 账户后续定仓参数。不会下单、平仓或切换执行账户。" : data.environment === "LIVE" ? "生产环境只读，所有资金参数修改与发布操作已禁用。" : "此账户尚未完成资金风控配置，当前仅可查看。";
  data.catalog.forEach(field => {
    const meta = FIELDS[field.key]; if (!meta) return;
    const row = text("div", "", "field"), label = document.createElement("label"), id = "field-" + field.key.replaceAll(".", "-");
    label.setAttribute("for", id); label.append(text("span", meta[0], "field-title"), text("span", meta[2], "field-help"));
    const input = document.createElement("input"); input.id = id; input.type = "text"; input.inputMode = "decimal"; input.maxLength = 80;
    input.value = displayValue(field.key, data.values[field.key]); input.disabled = !data.editable; input.setAttribute("aria-label", meta[0]);
    input.setAttribute("aria-describedby", id + "-error"); input.addEventListener("input", invalidate);
    input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); preview(); } });
    const wrapper = text("div", "", "input-unit"), error = text("span", "", "field-error"); error.id = id + "-error"; error.hidden = true;
    wrapper.append(input, text("span", meta[1])); row.append(label, wrapper, error);
    inputs.set(field.key, input); fieldErrors.set(field.key, error); $(meta[3] ? "advanced-fields" : "fields").append(row);
  });
  const history = data.history.filter(h => !h.deleted && h.version < data.version);
  $("history").replaceChildren(...(history.length ? history.map(h => new Option(`v${h.version} · ${date(h.at)}`, String(h.version))) : [new Option("暂无可恢复的版本", "")]));
  $("restore").disabled = !data.editable || !history.length;
  data.history.forEach(h => { const row = text("div", "", "history-item"), content = text("div", ""); content.append(text("p", h.reason || "未填写说明"), text("small", `${date(h.at)} UTC+8${h.version === data.version ? " · 当前版本" : ""}`)); row.append(text("span", `v${h.version}`, "tag"), content); $("history-list").append(row); });
  if (!data.history.length) empty("history-list", "暂无配置变更", "该账户没有已记录的参数版本。");
  $("full-policy").textContent = JSON.stringify(data.values, null, 2);
  $("risk-insight").textContent = `总预算 ${unitValue("capital.pool_fraction", data.values["capital.pool_fraction"])} · 单笔 ${unitValue("sizing.max_margin_fraction", data.values["sizing.max_margin_fraction"])}`;
  updateDraft();
}
function renderAnalytics(data) {
  analytics = data;
  const s = data.summary, has = s.settled_trades > 0;
  const metrics = [["已结算净收益 · USDT", has ? number(s.net_pnl) : "—", has ? `平均每笔 ${number(s.mean_net_pnl)} USDT` : "尚无已结算策略交易", has ? tone(s.net_pnl) : ""],
    ["胜率", has ? `${number(s.win_rate_pct, 1)}%` : "—", has ? `${s.wins ?? 0} 盈利 / ${s.losses ?? 0} 亏损 / ${s.breakeven ?? 0} 持平` : "暂无可计算样本", ""],
    ["已结算交易", String(s.settled_trades), `${s.unsettled_or_unvalued ?? 0} 笔未结算或未估值`, ""],
    ["中位持仓时长", duration(s.median_held_minutes), "仅统计已结算策略交易", ""]];
  metrics.forEach(([label, value, hint, color]) => { const item = text("div", "", "metric"); item.append(text("span", label, "metric-label"), text("strong", value, color), text("small", hint)); $("summary").append(item); });
  $("sample-badge").textContent = data.truncated ? "部分交易样本" : "当前窗口样本";
  $("trade-count").textContent = data.trades.length; $("trade-scope").textContent = `${data.trades.length} 条已读取${data.truncated ? " · 已截断" : ""}`;
  $("trade-source").replaceChildren(new Option("全部来源", ""), ...[...new Set(data.trades.map(t => t.source))].sort().map(s => new Option(source(s), s)));
  renderTrades(); renderSignals(data); renderEquity(data.equity, data.capital);
  simpleTable("groups", ["策略来源", "市场状态", "结算笔数", "净收益 U", "胜率", "持仓时长"], data.groups.map(g => {
    const identity = text("div", source(g.source)); identity.append(text("small", `${g.producer?.toUpperCase() || ""}${g.timeframe_minutes ? " · " + g.timeframe_minutes + "m" : ""} · ${g.event_type || "未记录事件"}`));
    identity.title = `策略 ${g.strategy_version}\n配置 ${g.config_digest}\n量化配置 ${g.quant_config_digest || "—"}`;
    return [identity, ({TREND_UP: "上行趋势", TREND_DOWN: "下行趋势", RANGE: "震荡", UNKNOWN: "未记录"})[g.regime] || g.regime, g.settled_trades, text("strong", g.settled_trades ? number(g.net_pnl) : "—", tone(g.net_pnl)), g.win_rate_pct === null ? "—" : `${number(g.win_rate_pct, 1)}%`, duration(g.median_held_minutes)];
  }));
  [...data.limitations, `交易读取上限 ${data.sample_limit} 条；${data.truncated ? "已截断，不是全量业绩" : "当前窗口未截断"}。`, `维护单排除 ${s.maintenance_excluded || 0} 笔；未结算/未估值 ${s.unsettled_or_unvalued || 0} 笔。`].forEach(n => $("limitations").append(text("li", n)));
  $("updated-at").textContent = `数据时间 ${date(data.as_of)} UTC+8`;
}
function renderTrades() {
  if (!analytics) return;
  const query = $("trade-search").value.trim().toUpperCase(), selectedSource = $("trade-source").value, result = $("trade-result").value;
  let rows = analytics.trades.filter(t => (!query || t.symbol.toUpperCase().includes(query)) && (!selectedSource || t.source === selectedSource) &&
    (!result || (result === "maintenance" ? t.maintenance : result === "unknown" ? t.net_pnl === null : !t.maintenance && t.net_pnl !== null && (result === "win" ? Number(t.net_pnl) > 0 : result === "loss" ? Number(t.net_pnl) < 0 : Number(t.net_pnl) === 0))));
  const order = $("trade-sort").value;
  if (order !== "recent") rows = [...rows].sort((a, b) => { const k = order === "duration" ? "held_minutes" : "net_pnl"; if (a[k] === null) return b[k] === null ? 0 : 1; if (b[k] === null) return -1; return (Number(a[k]) - Number(b[k])) * (order === "loss" ? 1 : -1); });
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE)); tradePage = Math.min(pages, Math.max(1, tradePage));
  const pageRows = rows.slice((tradePage - 1) * PAGE_SIZE, tradePage * PAGE_SIZE);
  simpleTable("trades", ["币种 / 方向", "来源", "净收益 U", "费用 U", "持仓时长", "状态", ""], pageRows.map(t => {
    const symbol = text("div", t.symbol); symbol.append(text("small", ["BUY", "LONG"].includes(t.side) ? "做多" : ["SELL", "SHORT"].includes(t.side) ? "做空" : t.side));
    const detail = text("button", "详情 →", "text-button"); detail.type = "button"; detail.setAttribute("aria-label", `查看 ${t.symbol} 交易详情`); detail.addEventListener("click", () => showTrade(t));
    return [symbol, text("span", source(t.source), "source-label"), text("strong", number(t.net_pnl), tone(t.net_pnl)), number(t.fees_usdt), duration(t.held_minutes), text("span", t.maintenance ? "维护交易" : t.net_pnl === null ? "未结算 / 未估值" : "已结算", "tag" + (t.net_pnl === null ? " warning" : "")), detail];
  }));
  if (!rows.length) empty("trades", analytics.trades.length ? "没有符合筛选条件的交易" : "这个范围暂无交易", analytics.trades.length ? "试试其他币种、来源，或点击“重置”清除筛选。" : "可以切换统计范围；未读取的数据不会按零收益处理。");
  $("trade-page-info").textContent = rows.length ? `${rows.length} 条匹配 · 第 ${tradePage} / ${pages} 页 · 每页 ${PAGE_SIZE} 条` : "0 条匹配";
  $("trade-prev").disabled = tradePage <= 1; $("trade-next").disabled = tradePage >= pages;
}
function showTrade(t) {
  $("trade-dialog-title").textContent = `${t.symbol} · ${["BUY", "LONG"].includes(t.side) ? "做多" : "做空"}`;
  const grid = text("dl", "", "detail-grid");
  [["净收益（已含费用）", `${number(t.net_pnl)} USDT`], ["手续费明细（不重复扣除）", `${number(t.fees_usdt)} USDT`], ["来源", source(t.source)], ["持仓时长", duration(t.held_minutes)],
    ["资金限制", t.sizing_explanation ? (t.sizing_explanation.binding_margin_limits || []).map(k => LIMITS[k] || k).join("、") || "未记录" : "旧数据未记录"],
    ["退出原因", t.exit_reason || "未记录"], ["采样最大浮盈 / 浮亏", t.excursions ? `${number(t.excursions.mfe_return_pct)}% / ${number(t.excursions.mae_return_pct)}%` : "未采样"], ["数据状态", t.maintenance ? "维护交易，排除策略统计" : t.net_pnl === null ? "未结算或无法估值" : "已结算"]].forEach(([label, value]) => { const item = document.createElement("div"); item.append(text("dt", label), text("dd", value)); grid.append(item); });
  const technical = document.createElement("details"); technical.append(text("summary", "技术记录与版本"), text("pre", JSON.stringify({id: t.id, strategy_version: t.strategy_version, policy_version: t.policy_version, config_digest: t.config_digest, event_type: t.event_type}, null, 2)));
  $("trade-detail").replaceChildren(grid, technical); $("trade-dialog").showModal();
}
function consumerLabel(value) { try { const p = JSON.parse(value).at(-1); return p === "s6" ? "多头策略" : p === "s8" ? "空头策略" : p; } catch (_) { return "策略消费者"; } }
function reasonInfo(code) { return REASONS[code] || REASONS[String(code).toUpperCase()] || ["其他处理条件", "展开技术详情查看原始处理原因。不会据此推算假设收益。"]; }
function renderSignals(data) {
  const receipts = [...data.receipts].sort((a, b) => b.receipts - a.receipts), max = Math.max(1, ...receipts.map(r => r.receipts));
  $("receipt-coverage").textContent = `最近 ${data.receipt_sample_count || 0} 份回执${data.receipt_sample_truncated ? " · 已截断，不是全量" : ""}，按策略消费者分别统计。`;
  receipts.forEach(r => {
    const [label, help] = reasonInfo(r.reason), row = text("div", "", "reason-row"), heading = text("div", "");
    heading.append(text("strong", r.outcome === "INTENT" ? "已生成开仓意图" : label), text("span", `${number(r.receipts, 0)} 份`, "reason-count"));
    const bar = document.createElement("progress"); bar.max = max; bar.value = r.receipts; bar.setAttribute("aria-label", `${label} ${r.receipts} 份回执`);
    const details = document.createElement("details"); details.append(text("summary", "技术详情"), text("p", `状态：${r.outcome} · 原因：${r.reason}`));
    row.append(heading, text("p", `${source(r.source)} · ${consumerLabel(r.consumer)} · ${r.outcome === "INTENT" ? "生成意图不等于已经成交。" : help}`), bar, details); $("receipts").append(row);
  });
  if (!receipts.length) empty("receipts", "暂无消费回执", "没有数据不等于没有收到信号，当前面板只统计已读取回执。");
  const limits = Object.entries(data.sizing_limits).sort((a, b) => b[1] - a[1]);
  limits.forEach(([key, count]) => { const row = text("div", "", "balance-row"); row.append(text("span", LIMITS[key] || key), text("strong", `${count} 次`)); $("limits").append(row); });
  if (!limits.length) empty("limits", "当前样本未记录限制解释", "部分旧决策没有定仓解释，不能解读为“没有资金限制”。");
  $("sizing-coverage").textContent = `${data.sizing_sample_count} 份决策${data.sizing_sample_truncated ? "，已截断" : ""}；多个约束同时生效时分别计数。`;
  $("signal-insight").textContent = receipts.length ? `${number(data.receipt_sample_count || 0, 0)} 份回执 · ${receipts[0].outcome === "INTENT" ? "已生成开仓意图" : reasonInfo(receipts[0].reason)[0]}` : "暂无已读取回执";
}
function renderEquity(data, capital) {
  if (!data?.latest) {
    empty("equity-chart", data?.status === "QUERY_TIMEOUT" ? "净值历史读取超时" : "暂无有效净值采样", data?.status === "QUERY_TIMEOUT" ? "其他交易统计仍可查看，可稍后刷新重试。" : "切换账户或统计范围后，数据会在这里展示。");
    $("equity-badge").textContent = data?.status === "QUERY_TIMEOUT" ? "暂不可用" : "暂无采样";
    $("equity-coverage").textContent = "不沿用其他账户数据，不把缺失净值当作零。"; empty("equity-summary", "暂无资金快照", "不会用演练本金代替真实余额。"); return;
  }
  const last = data.latest;
  $("equity-total").textContent = number(last.equity);
  $("equity-badge").textContent = data.fresh ? "最新采样" : "历史快照 · 已过期"; $("equity-badge").className = "badge" + (data.fresh ? "" : " warning");
  [["账户净值", last.equity], ["浮动损益", last.unrealized], ["可用保证金", last.available_margin], ["已用保证金", last.used_margin], ["钱包余额", last.wallet], ["风险预算基数", capital?.base], ["保证金 / 净值", last.margin_to_equity_pct, "%"], ["原始采样峰值降幅", data.raw_sampled_peak_decline_pct, "%"]].forEach(([label, value, unit]) => { const row = text("div", "", "balance-row"); row.append(text("span", label), text("strong", number(value) + (unit || " U"), label === "浮动损益" ? tone(value) : "")); $("equity-summary").append(row); });
  $("equity-coverage").textContent = `${data.samples} 个有效样本${data.scan_truncated ? " · 读取已截断" : ""} · 最大间隔 ${duration(data.max_gap_ms === null ? null : data.max_gap_ms / 60000)}。净值受资金进出影响，不是策略收益曲线。`;
  simpleTable("equity-cash", ["现金事件", "币种", "笔数", "金额"], data.cash.groups.map(g => [({TRANSFER: "资金划转", COMMISSION: "手续费", REALIZED_PNL: "已实现损益", FUNDING_FEE: "资金费"})[g.type] || g.type, g.currency, g.events, number(g.amount)]));
  [...data.limitations, `实际采样范围 ${date(data.first_at)} 至 ${date(data.last_at)} UTC+8；现金事件 ${data.cash.events} 条${data.cash.truncated ? "（已截断）" : ""}。`, `对账残差 ${number(data.cash.wallet_reconciliation_residual)} U；现金流完整性未获证明，残差为零也不等于完整。`, `排除记录：${JSON.stringify(data.excluded)}；时钟倒退：${data.clock_regression ? "有" : "未发现"}。`].forEach(note => $("equity-notes").append(text("li", note)));
  chartPoints = (data.chart || []).filter(p => Number.isFinite(Number(p.equity)) && Number.isFinite(Number(p.wallet)) && Number.isFinite(p.at_ms));
  drawChart();
}
function svgElement(tag, attributes, value) { const el = document.createElementNS("http://www.w3.org/2000/svg", tag); Object.entries(attributes).forEach(([k, v]) => el.setAttribute(k, String(v))); if (value !== undefined) el.textContent = value; return el; }
function selectPoint(index) {
  const p = chartPoints[index]; if (!p) return;
  $("chart-point").value = index; $("chart-readout").textContent = `${date(p.at)} UTC+8 · 净值 ${number(p.equity)} U · 钱包 ${number(p.wallet)} U`;
}
function drawChart() {
  if (!chartPoints.length) return;
  const values = chartPoints.flatMap(p => [Number(p.equity), Number(p.wallet)]), low = Math.min(...values), high = Math.max(...values), pad = (high - low) * .12 || Math.max(1, Math.abs(high) * .001), lo = low - pad, hi = high + pad;
  const first = chartPoints[0].at_ms, span = chartPoints.at(-1).at_ms - first || 1, x = t => 70 + (t - first) / span * 790, y = v => 205 - (v - lo) / (hi - lo) * 170;
  const svg = svgElement("svg", {viewBox: "0 0 900 250", role: "img", "aria-label": "账户净值与钱包余额采样点，蓝色为净值，灰色为钱包；未对采样间隔插值"});
  for (let i = 0; i < 4; i++) { const v = lo + (hi - lo) * i / 3; svg.append(svgElement("line", {x1: 70, x2: 860, y1: y(v), y2: y(v), class: "chart-grid"}), svgElement("text", {x: 59, y: y(v) + 4, "text-anchor": "end", class: "chart-axis"}, number(v, 1))); }
  [0, Math.floor((chartPoints.length - 1) / 2), chartPoints.length - 1].filter((v, i, a) => a.indexOf(v) === i).forEach(i => svg.append(svgElement("text", {x: x(chartPoints[i].at_ms), y: 231, "text-anchor": i === 0 ? "start" : i === chartPoints.length - 1 ? "end" : "middle", class: "chart-axis"}, date(chartPoints[i].at))));
  chartPoints.forEach((p, index) => { [["wallet", "#7890b0"], ["equity", "#a4ceff"]].forEach(([key, color]) => { const dot = svgElement("circle", {cx: x(p.at_ms), cy: y(Number(p[key])), r: key === "equity" ? 2.8 : 1.7, fill: color}); dot.append(svgElement("title", {}, `${date(p.at)} UTC+8 · ${key === "equity" ? "净值" : "钱包"} ${number(p[key])} U`)); dot.addEventListener("pointerenter", () => selectPoint(index)); dot.addEventListener("click", () => selectPoint(index)); svg.append(dot); }); });
  $("equity-chart").replaceChildren(svg); $("chart-point").max = chartPoints.length - 1; $("chart-point").disabled = chartPoints.length < 2; selectPoint(chartPoints.length - 1);
}

async function load() {
  const serial = ++generation, registry = $("account").value; clear();
  selected = {environment: $("environment").value, account: registry, days: $("days").value};
  workspace?.setContext(selected); workspace?.describe(accountName(), selected.environment);
  $("error-banner").hidden = true; $("environment-badge").textContent = selected.environment === "LIVE" ? "生产环境 · 只读" : "Testnet · 测试环境"; $("environment-badge").className = "badge" + (selected.environment === "LIVE" ? " live" : "");
  $("account-context").textContent = `${accountName()}${registry && registry === executionRegistry && selected.environment === "SANDBOX" ? " · 页面载入时的执行账户" : " · 当前查看账户"}`;
  if (!registry) { $("status").textContent = "该环境没有可选账户，可前往账户管理添加。"; document.body.classList.remove("loading"); $("refresh").disabled = false; return; }
  $("status").textContent = "正在读取账户数据…"; document.body.classList.add("loading"); $("refresh").disabled = true;
  try {
    const [p, a] = await Promise.all([api(targetPath(registry, "policy")), api(targetPath(registry, "analytics") + `?days=${selected.days}`)]);
    if (serial !== generation) return;
    if (p.registry_id !== registry || a.registry_id !== registry || p.environment !== $("environment").value || a.environment !== p.environment) throw new Error("账户响应不匹配，已拒绝展示，请重试。");
    renderPolicy(p); renderAnalytics(a); $("status").textContent = "数据已更新";
  } catch (e) { if (serial === generation) { clear(); $("status").textContent = "加载失败，未展示旧数据"; $("error-message").textContent = e.message; $("error-banner").hidden = false; } }
  finally { if (serial === generation) { document.body.classList.remove("loading"); $("refresh").disabled = false; } }
}
function chooseAccounts(preferred) { const list = accounts.filter(a => a.environment === $("environment").value); $("account").replaceChildren(...list.map(a => new Option(a.alias, a.registry_id))); if (list.some(a => a.registry_id === preferred)) $("account").value = preferred; return load(); }
function changeContext(kind) {
  if (!mayDiscard()) { $("environment").value = selected.environment; $("account").value = selected.account; $("days").value = selected.days; return; }
  if (kind === "environment") chooseAccounts($("environment").value === "SANDBOX" ? executionRegistry : null); else load();
}
async function preview(restore = false) {
  if (!policy?.editable || publishing || pending?.body) return;
  const current = draft(); if (!restore && (current.invalid.size || !Object.keys(current.changes).length)) { updateDraft(); return; }
  if (restore && dirty && !mayDiscard()) return;
  const serial = generation, registry = $("account").value, version = policy.version, restoreVersion = restore ? Number($("history").value) : null;
  invalidate(); const edited = revision; $("preview").disabled = true; $("preview-result").textContent = "正在校验参数并计算预览…"; showTab("risk");
  try {
    const p = await api(targetPath(registry, "preview"), {expected_version: version, changes: restore ? {} : current.changes, restore_version: restoreVersion});
    if (serial !== generation || edited !== revision) return;
    if (p.registry_id !== registry || p.version !== version) throw new Error("预览账户或版本不匹配。");
    pending = {...p, request_id: crypto.randomUUID(), body: null}; $("preview-result").replaceChildren(); $("preview-result").className = "";
    $("preview-context").textContent = `${accountName()} · v${version} → 新版本${restore ? " · 恢复历史参数" : ""}`;
    Object.entries(p.diff).filter(([k]) => k !== "sizing.pool_fraction").forEach(([k, v]) => { const row = text("div", "", "preview-row"), amount = text("span", ""); amount.append(text("del", unitValue(k, v.before)), text("span", " → "), text("strong", unitValue(k, v.after))); row.append(text("span", FIELDS[k]?.[0] || k), amount); $("preview-result").append(row); });
    $("preview-result").append(text("p", "仓位示例：1000 USDT、3 倍杠杆、评分 100。仅作算术对比，不是收益回测。", "preview-callout"));
    p.after.rows.forEach((r, i) => { const row = text("div", "", "preview-row"); row.append(text("span", `止损距离 ${number(Number(r.stop_fraction) * 100)}%`), text("strong", `${number(p.before.rows[i].explanation.planned_margin)} → ${number(r.explanation.planned_margin)} U`)); $("preview-result").append(row); });
    const historical = p.historical_sizing;
    if (historical) { const details = document.createElement("details"); details.append(text("summary", `历史定仓重算：${historical.replayed} 笔可复现`), text("p", `最近 ${historical.sampled_decisions || 0} 份决策，仓位增大 ${historical.changes.LARGER || 0}、缩小 ${historical.changes.SMALLER || 0}、不变 ${historical.changes.UNCHANGED || 0}。${historical.limitations} 跳过：${JSON.stringify(historical.skipped)}`, "caption")); $("preview-result").append(details); }
    const notes = text("ul", "", "preview-notes"); p.warnings.forEach(w => notes.append(text("li", w))); $("preview-result").append(notes);
    $("publish-fields").hidden = false; $("dirty-dot").hidden = false; $("discard").disabled = false; expiry();
  } catch (e) { if (serial === generation && edited === revision) $("preview-result").textContent = e.message; }
  finally { if (serial === generation && edited === revision) updateDraft(); }
}
function expiry() {
  if (!pending) return;
  const seconds = Math.max(0, Math.ceil((pending.preview_expires_at_ms - Date.now()) / 1000));
  $("preview-expiry").textContent = pending.body ? "如发布响应丢失，可重试同一请求；不会创建重复配置版本。" : seconds ? `预览剩余 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒有效` : "预览已过期，请重新预览。";
  $("apply").disabled = publishing || (!pending.body && (!seconds || !$("reason").value.trim()));
  $("apply").textContent = pending.body ? "核对 / 重试本次发布" : "确认发布到 Testnet";
}
async function apply() {
  if (!pending || publishing || !policy?.editable) return;
  const current = pending, serial = generation, reason = $("reason").value.trim();
  if (!current.body && (!reason || current.preview_expires_at_ms <= Date.now())) { expiry(); $("apply-status").textContent = !reason ? "请填写变更原因。" : "预览已过期，请重新预览。"; return; }
  if (!window.confirm(`确认更新“${accountName()}”的 Testnet 资金配置？\n当前版本 v${current.version}。不会下单或切换执行账户。`)) return;
  current.body ||= {expected_version: current.version, changes: current.changes, restore_version: current.restore_version, preview_digest: current.preview_digest, preview_expires_at_ms: current.preview_expires_at_ms, reason, request_id: current.request_id};
  publishing = true; updateDraft(); expiry(); $("apply-status").textContent = "正在发布，请勿重复操作…";
  try {
    const result = await api(targetPath(current.registry_id, "apply"), current.body);
    if (serial !== generation) return;
    dirty = false; pending = null; await load();
    if (generation === serial + 1) $("apply-status").textContent = `已保存为 v${result.version}。执行器后续读取时生效，不代表已经产生订单。`;
  } catch (e) { if (serial === generation) { $("apply-status").textContent = `${e.message} 发布结果未确认，可重试同一请求或刷新核对版本。`; } }
  finally { publishing = false; updateDraft(); expiry(); }
}
TABS.forEach((tab, index) => { const button = $("tab-" + tab); button.addEventListener("click", () => showTab(tab)); button.addEventListener("keydown", e => { if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return; e.preventDefault(); const target = e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : (index + (e.key === "ArrowRight" ? 1 : -1) + TABS.length) % TABS.length; showTab(TABS[target], {focus: true}); }); });
document.querySelectorAll("[data-go]").forEach(link => link.addEventListener("click", e => { e.preventDefault(); showTab(link.dataset.go); }));
$("signal-shortcut").addEventListener("click", () => showTab("signals", {focus: true})); $("risk-shortcut").addEventListener("click", () => showTab("risk", {focus: true}));
$("environment").addEventListener("change", () => changeContext("environment")); $("account").addEventListener("change", () => changeContext("account")); $("days").addEventListener("change", () => changeContext("days"));
function refresh() { if (accountsReady) changeContext("refresh"); else start(); }
$("refresh").addEventListener("click", refresh); $("retry").addEventListener("click", refresh);
$("preview").addEventListener("click", () => preview()); $("restore").addEventListener("click", () => preview(true)); $("discard").addEventListener("click", discard); $("apply").addEventListener("click", apply); $("reason").addEventListener("input", expiry);
$("history").addEventListener("change", invalidate); $("chart-point").addEventListener("input", () => selectPoint(Number($("chart-point").value)));
["trade-search", "trade-source", "trade-result", "trade-sort"].forEach(id => $(id).addEventListener(id === "trade-search" ? "input" : "change", () => { tradePage = 1; renderTrades(); }));
$("reset-filters").addEventListener("click", () => { $("trade-search").value = ""; $("trade-source").value = ""; $("trade-result").value = ""; $("trade-sort").value = "recent"; tradePage = 1; renderTrades(); });
$("trade-prev").addEventListener("click", () => { tradePage--; renderTrades(); }); $("trade-next").addEventListener("click", () => { tradePage++; renderTrades(); }); $("close-trade").addEventListener("click", closeTrade);
$("trade-dialog").addEventListener("click", e => { if (e.target === $("trade-dialog")) { const b = $("trade-dialog").getBoundingClientRect(); if (e.clientX < b.left || e.clientX > b.right || e.clientY < b.top || e.clientY > b.bottom) closeTrade(); } });
window.addEventListener("beforeunload", e => { if (dirty || pending) { e.preventDefault(); e.returnValue = ""; } }); window.addEventListener("hashchange", () => showTab(window.location.hash.slice(1), {writeHash: false}));
setInterval(expiry, 1000); showTab(window.location.hash.slice(1), {writeHash: false});
async function loadReview() {
  const registry = $("account").value, environment = $("environment").value, days = $("days").value;
  if (!registry) return;
  const serial = generation, request = ++reviewSequence;
  $("review-load").disabled = true;
  ["review-variants", "review-trades", "review-notes", "review-risk-summary", "review-risk-groups"].forEach(id => $(id).replaceChildren());
  $("review-status").textContent = "正在生成只读复盘…不影响运行参数。";
  try {
    const data = await api(targetPath(registry, "review") + `?days=${encodeURIComponent(days)}`);
    if (serial !== generation || request !== reviewSequence) return;
    if (data.registry_id !== registry || data.environment !== environment) throw new Error("复盘账户不匹配，已丢弃结果。");
    if (data.status !== "AVAILABLE") throw new Error("分析任务正忙，请稍后再试；交易继续运行。");
    $("review-status").textContent = `${accountName()} · ${date(data.as_of)} UTC+8 · ${data.summary.settled_trades} 笔已结算 · 实际净收益 ${number(data.summary.net_pnl)} U${data.cached ? " · 缓存" : ""}${data.truncated ? " · 样本已截断，不是全量统计" : ""}。对照不是回测收益；未知样本不默认放行。`;
    simpleTable("review-variants", ["研究假设", "保留 / 排除 / 未知", "已知样本原净收益", "保留样本原净收益", "排除样本原净收益"], data.comparisons.map(r => [r.label, `${r.kept_count} / ${r.excluded_count} / ${r.unknown_count}`, `${number(r.known_baseline.net_pnl)} U`, `${number(r.kept_actual.net_pnl)} U`, `${number(r.excluded_actual.net_pnl)} U`]));
    const entryNames = {RIGHT_MOMENTUM: "顺势动量", S6B_TREND_TAKEOVER: "强趋势接管", LEFT_REVERSAL: "左侧反转"};
    const risk = data.risk_summary;
    $("review-risk-summary").textContent = risk ? `历史每笔净收益 ${number(risk.historical_mean_net_usdt)} U（${risk.settled_count} 笔）；净 R 均值 ${number(risk.historical_mean_net_r)} R（有效 ${risk.r_sample_count} 笔 / 缺失 ${risk.r_missing_count} 笔）；净亏损超过初始毛风险 ${risk.loss_beyond_initial_risk_count} 笔。R 不是账户收益率；历史均值不是盈利预测，费用不重复扣除。` : "当前报告未提供风险证据，不能计算 R。";
    simpleTable("review-risk-groups", ["来源 / 模式 / 方向", "策略版本 / 参数摘要", "结算数 / R 有效数", "历史每笔净收益 U / 净 R", "实际平均盈利 / 平均亏损 U / 盈亏比"], (data.groups || []).map(g => [
      `${source(g.source)} / ${entryNames[g.entry_mode] || g.entry_mode || "未记录"} / ${g.side}`,
      `${g.strategy_version || "未记录"} / ${g.config_digest || "未记录"}`,
      `${g.risk?.settled_count ?? g.settled_trades} / ${g.risk?.r_sample_count ?? "—"}`,
      `${number(g.risk?.historical_mean_net_usdt)} / ${number(g.risk?.historical_mean_net_r)}`,
      `${number(g.risk?.average_net_win_usdt)} / ${number(g.risk?.average_net_loss_usdt)} / ${number(g.risk?.realized_payoff_ratio)}`
    ]));
    const exitNames = {PEAK_DRAWDOWN: "盈利回撤退出", EMERGENCY_LOSS: "紧急止损", EARLY_LOSS_MOMENTUM_WEAK: "早期亏损且动量转弱", TIME_STOP: "时间止损", LOW_YIELD_STAGNATION: "低收益停滞", ONE_HOUR_REVERSAL: "小时趋势反转", VENUE_PROTECTION_CHILD: "交易所原生保护", PARTIAL_TAKE_PROFIT: "分批止盈"};
    simpleTable("review-trades", ["币种 / 来源", "开仓 / 平仓 UTC+8", "入场 / 评分 / 计划杠杆", "离场原因", "距前次平仓", "净收益 / 费用 U", "初始毛风险 U / 实现净 R / 入场风险偏差", "T+60 相对全部平仓成交均价"], data.trades.slice().reverse().map(t => [
      `${t.symbol} · ${source(t.source)}`, `${date(t.opened_ms)} / ${date(t.closed_ms)}`,
      `${entryNames[t.entry_mode] || t.entry_mode || "未记录"} / ${number(t.score, 0)} / ${number(t.leverage, 0)}×`, exitNames[t.exit_reason] || t.exit_reason || "未记录 / 未平仓",
      t.prior_close_gap_ms == null ? "前序记录不足" : duration(t.prior_close_gap_ms / 60000),
      `${number(t.net_pnl)} / ${number(t.fees_usdt)}`,
      t.risk?.status === "AVAILABLE" ? `${number(t.risk.initial_stop_risk_usdt)} / ${number(t.risk.realized_net_r)} R / ${number(t.risk.entry_risk_drift_pct)}%` : "风险证据缺失或不一致，无法计算",
      t.followup_60_return_pct == null ? "尚无观测" : t.followup_60_basis && t.followup_60_basis !== "ALL_CLOSE_FILL_VWAP_NOT_FINAL_FLAT_MARK" ? "观测口径未知，不展示收益" : `${number(t.followup_60_return_pct)}%（非清仓后收益，不能据此判断卖飞）`
    ]));
    const notes = text("ul", ""); data.limitations.forEach(note => notes.append(text("li", note))); $("review-notes").replaceChildren(notes);
  } catch (e) { if (serial === generation && request === reviewSequence) $("review-status").textContent = e.message; }
  finally { if (serial === generation && request === reviewSequence) $("review-load").disabled = false; }
}
$("review-load").addEventListener("click", loadReview);
async function loadShadow() {
  const registry = $("account").value, environment = $("environment").value;
  if (!registry) return;
  const serial = generation, request = ++shadowSequence;
  $("shadow-load").disabled = true;
  ["shadow-rows", "shadow-notes", "shadow-coverage", "shadow-evaluation-status", "shadow-groups"].forEach(id => $(id).replaceChildren());
  $("shadow-status").textContent = "正在读取旁路记录…";
  try {
    const data = await api(targetPath(registry, "shadow") + `?days=${encodeURIComponent($("days").value)}`);
    if (serial !== generation || request !== shadowSequence) return;
    if (data.registry_id !== registry || data.environment !== environment) throw new Error("账户范围不匹配，未展示数据");
    if (data.status === "BUSY") throw new Error("旁路报告正在读取，请稍后重试。");
    const labels = {OBSERVING: "正在观测", NO_ACTIVE_SANDBOX: "没有运行中的测试账户", NOT_OBSERVING_SELECTED_ACCOUNT: "当前未观测此账户", NOT_INSTALLED: "尚未部署", NOT_STARTED: "观测尚未启动", SANDBOX_ONLY: "只在测试环境观测", STORAGE_BUDGET_EXHAUSTED: "研究存储已达限额", ROUTE_CHANGED: "账户切换中", OBSERVER_READ_OR_WRITE_FAILED: "观测读写失败"};
    const states = {READY: "结构满足（非下单许可）", WAIT: "结构尚未恢复", UNKNOWN: "证据不足", NOT_APPLICABLE: "非同向再入场"};
    const actualStates = {SETTLED: "已最终结算", UNLINKED_DECISION: "决策关联不完整", NO_INTENT: "尚无交易意图", UNFILLED: "尚无开仓成交", OPEN_OR_PARTIAL_EXIT: "持仓中 / 部分平仓", AWAITING_TERMINAL_ORDERS: "等待订单终态", AWAITING_SETTLEMENT: "已平仓，等待结算", SETTLEMENT_EVIDENCE_INCOMPLETE: "结算证据不完整", SETTLEMENT_REVISION_CHANGED: "台账版本变化，等待重新结算", MAINTENANCE_EXCLUDED: "维护交易不计入", LATER_ORDER_STATE: "订单状态超出快照时间", INCONSISTENT_FILLS: "成交数量不一致"};
    $("shadow-status").textContent = `${accountName()} · ${data.health.stale ? "观测心跳过期，请检查旁路进程" : labels[data.health.status] || data.health.status} · ${data.rows.length} 条${data.truncated ? "（仅最近 100 条）" : ""}${data.health.heartbeat_ms ? ` · 心跳 ${date(new Date(data.health.heartbeat_ms).toISOString())} UTC+8` : ""}。仅观察，未启用开仓阻断。`;
    const coverage = data.coverage;
    $("shadow-coverage").textContent = !coverage || coverage.status === "UNAVAILABLE" ? "近期覆盖核对不可用：未运行、心跳过期或所选账户并非观测账户，不能据此判断无漏采。" : `近 5 分钟窗口${coverage.status === "NO_NEW_OPEN" ? "没有新 OPEN 决策" : `：新 OPEN ${coverage.decisions} 条，当前参数已采到 ${coverage.recorded} 条，时限内待观测 ${coverage.pending} 条，超时未匹配 ${coverage.unobserved_over_lag} 条`}${coverage.truncated ? "（已截断，仅最近 500 条）" : ""}。按数据库写入时间核对；切账户、停机和限额都可能产生缺口，不代表交易被阻断。`;
    const groups = data.evaluation?.groups || [];
    $("shadow-evaluation-status").textContent = groups.length ? "以下仅为本页样本的原交易结果，分参数版本统计；WAIT 后原交易盈利、READY 后原交易亏损均需复核，不能直接推算改策略后的收益。未结算不计入胜率。" : "尚无可评估的旁路样本；不以历史回放或 0 盈亏冒充实时结算结果。";
    simpleTable("shadow-groups", ["影子参数 / 原策略版本", "来源 / 方向 / 原参数", "结构判断 / 机会记录", "观测 / 已结算", "原交易盈 / 亏 / 平 / 胜率", "原交易净收益 U", "WAIT 后盈利 / READY 后亏损"], groups.map(g => [
      `${g.policy_digest} / ${g.strategy_version}`, `${g.source} / ${g.side} / ${g.config_digest}`, `${states[g.status] || g.status} / ${g.claim}`,
      `${g.observations} / ${g.settled}`, `${g.wins} / ${g.losses} / ${g.breakeven} / ${g.actual_win_rate_pct == null ? "未形成结算样本" : `${number(g.actual_win_rate_pct)}%`}`,
      g.actual_net_pnl == null ? "待结算" : number(g.actual_net_pnl), `${g.wait_with_actual_profit} / ${g.ready_with_actual_loss}`
    ]));
    simpleTable("shadow-rows", ["币种 / 方向 / 来源", "决策 / 观测 UTC+8", "延迟秒", "结构判断", "原因 / 路径", "研究机会去重", "原交易状态 / 已结算净收益 U"], data.rows.map(r => [
      `${r.symbol} / ${r.side} / ${r.source || "—"}`, `${date(new Date(r.decided_ms).toISOString())} / ${date(new Date(r.observed_ms).toISOString())}`,
      number((r.observed_ms-r.decided_ms)/1000), states[r.status] || r.status, `${r.reason} / ${r.path || "—"}`, r.claim === "DUPLICATE_OPPORTUNITY" ? "同一结构机会已记录" : r.claim === "RECORDED" ? "首次结构机会" : "未认定机会",
      `${actualStates[r.actual?.state] || "尚无结果关联"} / ${r.actual?.net_pnl == null ? "—" : number(r.actual.net_pnl)}`
    ]));
    const notes = text("ul", ""); data.limitations.forEach(note => notes.append(text("li", note))); $("shadow-notes").replaceChildren(notes);
  } catch (e) { if (serial === generation && request === shadowSequence) $("shadow-status").textContent = e.message; }
  finally { if (serial === generation && request === shadowSequence) $("shadow-load").disabled = false; }
}
$("shadow-load").addEventListener("click", loadShadow);
async function start() {
  const serial = ++generation;
  $("error-banner").hidden = true; $("status").textContent = "正在读取账户列表…";
  try {
    const response = await api("/api/accounts");
    if (serial !== generation) return;
    accounts = response.accounts; accountsReady = true;
    try { const route = await api("/api/accounts/execution/SANDBOX"); if (serial === generation && ["ACTIVE", "DRAINING"].includes(route.phase)) executionRegistry = route.target_registry; } catch (_) { /* Account list remains independently usable. */ }
    if (serial === generation) {
      const requested = workspace?.requested;
      if (requested) { $("environment").value = requested.environment; $("days").value = requested.days; }
      const valid = requested?.account && accounts.some(a => a.registry_id === requested.account && a.environment === requested.environment);
      await chooseAccounts(valid ? requested.account : $("environment").value === "SANDBOX" ? executionRegistry : null);
      if (requested?.invalid || (requested?.account && !valid)) { $("error-message").textContent = "链接中的账户不可用，已回到当前可查看账户；请核对顶部选择。"; $("error-banner").hidden = false; }
    }
  } catch (e) { if (serial === generation) { accountsReady = false; $("status").textContent = "账户列表加载失败，请重试"; $("error-message").textContent = e.message; $("error-banner").hidden = false; } }
}
start();
