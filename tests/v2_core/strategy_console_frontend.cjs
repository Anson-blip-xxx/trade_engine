"use strict";
const fs = require("fs"), vm = require("vm"), assert = require("assert");
class Element {
  constructor() { this.value = ""; this.children = []; this.listeners = {}; this.disabled = false; this.content = ""; this.attributes = {}; this.classList = {add() {}, remove() {}}; }
  set textContent(v) { this.content = String(v); this.children = []; }
  get textContent() { return this.content + this.children.map(c => c.textContent).join(""); }
  set innerHTML(_) { throw Error("Unsafe HTML rendering"); }
  replaceChildren(...items) { this.children = items; this.content = ""; this.value = items[0]?.value || ""; }
  append(...items) { this.children.push(...items); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(k, v) { this.attributes[k] = v; }
  focus() { this.focused = true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}
const elements = {}, requests = [], events = {}; let uuid = 0, allowConfirm = true;
const get = id => elements[id] ?? (elements[id] = new Element());
get("environment").value = "SANDBOX"; get("days").value = "14";
const ctx = {document: {getElementById: get, createElement: () => new Element(), createElementNS: () => new Element(), querySelectorAll: () => [], body: new Element()},
  Option: function(t, v) { this.textContent = t; this.value = v; },
  window: {confirm: () => allowConfirm, location: {hash: ""}, history: {replaceState() {}}, addEventListener: (k, f) => events[k] = f},
  crypto: {randomUUID: () => `request-${++uuid}`}, console, setInterval: () => 1,
  fetch: (path, args) => new Promise(resolve => requests.push({path, args, resolve}))};
vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const run = s => vm.runInContext(s, ctx);
const clone = v => JSON.parse(JSON.stringify(v));
const tick = () => new Promise(r => setImmediate(r));
const respond = (i, data, ok = true) => { assert(requests[i], `Missing request ${i}`); requests[i].resolve({ok, status: ok ? 200 : 503, json: async () => data}); };
const fire = (id, name = "click", e = {}) => get(id).listeners[name](e);
const accounts = [{registry_id: "A", alias: "<img onerror=bad>", environment: "SANDBOX"}, {registry_id: "B", alias: "Beta", environment: "SANDBOX"}, {registry_id: "C", alias: "Live", environment: "LIVE"}];
const defaults = {"capital.pool_fraction": "0.70", "sizing.pool_fraction": "0.70", "sizing.max_margin_fraction": "0.05", "sizing.risk_fraction": "0.005", "portfolio_risk.max_fraction": "0.03", "sizing.min_allocation": "0.10", "sizing.max_allocation": "0.20", "capital.margin_buffer": "1.1", "sizing.cost_buffer_fraction": "0.001"};
function policy(id = "A", env = "SANDBOX", version = 1) { return {registry_id: id, environment: env, version, editable: env === "SANDBOX", values: {...defaults}, catalog: Object.keys(defaults).map(key => ({key, minimum: "0.000001", maximum: key === "capital.margin_buffer" ? "3" : key.includes("pool_fraction") ? "0.7" : "1"})), history: []}; }
function analytics(id = "A", n = 0, env = "SANDBOX") { return {registry_id: id, environment: env, summary: {settled_trades: n, net_pnl: String(n), mean_net_pnl: null, win_rate_pct: null}, sizing_limits: {}, sizing_sample_count: 0, receipts: [], groups: [], trades: [], limitations: [], capital: {fresh: false}, sample_limit: 500, as_of: "2026-09-28T12:00:00+08:00",
  equity: n ? {latest: {equity: String(n), unrealized: "-2", available_margin: "10"}, raw_sampled_peak_decline_pct: "5", first_at: "start", last_at: "end", samples: 2, max_gap_ms: 10, fresh: false,
  cash: {groups: [{type: "<img onerror=bad>", currency: "USDT", events: 1, amount: "1"}], events: 1, wallet_reconciliation_residual: "0"}, limitations: ["not strategy return"], excluded: {}, chart: [{at_ms: 10, at: "first", equity: "1", wallet: "2"}, {at_ms: 20, at: "last", equity: String(n), wallet: String(n)}]} : null}; }
function preview(id = "A", after = "0.1", version = 1) { return {registry_id: id, environment: "SANDBOX", version, preview_digest: "abc", preview_expires_at_ms: Date.now() + 300000, changes: {"sizing.max_margin_fraction": after}, diff: {"sizing.max_margin_fraction": {before: "0.05", after}}, restore_version: null, before: {rows: []}, after: {rows: []}, warnings: []}; }
function edit(value, key = "sizing.max_margin_fraction") { const input = run(`inputs.get(${JSON.stringify(key)})`); input.value = value; input.listeners.input(); }
async function boot(p = policy(), a = analytics()) { respond(0, {accounts}); await tick(); respond(1, {phase: "ACTIVE", target_registry: "A"}); await tick(); respond(2, p); respond(3, a); await tick(); }

const scenarios = {
  async reentry_shadow() {
    await boot(); fire("shadow-load"); await tick();
    const response = id => ({registry_id: id, environment: "SANDBOX", health: {status: "OBSERVING", heartbeat_ms: 1780000000000}, rows: [{symbol: "<img onerror=bad>", side: "BUY", source: "s3", decided_ms: 1780000000000, observed_ms: 1780000001000, status: "WAIT", reason: "STRUCTURE_NOT_REARMED", claim: "NOT_READY"}], limitations: ["不能当作收益"]});
    assert(requests[4].path.endsWith("/strategy/shadow?days=14"));
    get("account").value = "B"; fire("account", "change"); await tick();
    respond(5, policy("B")); respond(6, analytics("B")); await tick();
    respond(4, response("A")); await tick(); assert.equal(get("shadow-rows").children.length, 0);
    fire("shadow-load"); await tick(); respond(7, response("B")); await tick();
    assert(get("shadow-status").textContent.includes("Beta")); assert(get("shadow-rows").textContent.includes("结构尚未恢复"));
    assert(get("shadow-rows").textContent.includes("<img onerror=bad>"));
    fire("shadow-load"); await tick(); respond(8, response("A")); await tick();
    assert(get("shadow-status").textContent.includes("不匹配")); assert.equal(get("shadow-rows").children.length, 0);
    assert(requests.every(r => r.args.method === "GET" && !r.args.body));
  },
  async shadow_evaluation() {
    await boot(); fire("shadow-load"); await tick();
    const response = {registry_id: "A", environment: "SANDBOX", health: {status: "OBSERVING"}, coverage: {status: "RECENT_WINDOW", decisions: 3, recorded: 1, pending: 1, unobserved_over_lag: 1},
      rows: [{symbol: "BTCUSDT", side: "BUY", source: "s3", decided_ms: 1780000000000, observed_ms: 1780000001000, status: "WAIT", reason: "STRUCTURE_NOT_REARMED", claim: "NOT_READY", actual: {state: "AWAITING_SETTLEMENT", net_pnl: null}}],
      evaluation: {groups: [{policy_digest: "<img onerror=bad>", strategy_version: "v1", source: "s3", side: "BUY", config_digest: "frozen", status: "WAIT", claim: "NOT_READY", observations: 1, settled: 0, wins: 0, losses: 0, breakeven: 0, actual_win_rate_pct: null, actual_net_pnl: null, wait_with_actual_profit: 0, ready_with_actual_loss: 0}]}, limitations: []};
    respond(4, response); await tick();
    assert(get("shadow-coverage").textContent.includes("超时未匹配 1 条"));
    assert(get("shadow-rows").textContent.includes("已平仓，等待结算 / —"));
    assert(get("shadow-groups").textContent.includes("未形成结算样本"));
    assert(get("shadow-groups").textContent.includes("<img onerror=bad>"));
    assert(get("shadow-evaluation-status").textContent.includes("不能直接推算"));
    fire("shadow-load"); await tick(); respond(5, {registry_id: "A", environment: "SANDBOX", status: "BUSY"}); await tick();
    assert(get("shadow-status").textContent.includes("稍后重试")); assert(!get("shadow-load").disabled); assert.equal(get("shadow-groups").children.length, 0);
    fire("shadow-load"); await tick(); get("account").value = "B"; fire("account", "change"); await tick();
    respond(7, policy("B")); respond(8, analytics("B")); await tick(); respond(6, response); await tick();
    for (const id of ["shadow-coverage", "shadow-evaluation-status", "shadow-groups"]) assert.equal(get(id).children.length, 0);
    assert(requests.every(r => r.args.method === "GET"));
  },
  async risk_review() {
    await boot(); fire("review-load"); await tick();
    const risk = {settled_count: 3, historical_mean_net_usdt: "3.5", historical_mean_net_r: "0.35", r_sample_count: 2, r_missing_count: 1, loss_beyond_initial_risk_count: 1, average_net_win_usdt: "20", average_net_loss_usdt: "10", realized_payoff_ratio: "2"};
    respond(4, {registry_id: "A", environment: "SANDBOX", status: "AVAILABLE", as_of: "2026-09-30T19:00:00+08:00", summary: {settled_trades: 3, net_pnl: "10.5"}, risk_summary: risk,
      comparisons: [], groups: [{source: "s3", side: "BUY", strategy_version: "v1", config_digest: "<img onerror=bad>", risk}],
      trades: [{symbol: "BTCUSDT", risk: {status: "AVAILABLE", initial_stop_risk_usdt: "10", realized_net_r: "0.35", entry_risk_drift_pct: "0"}}, {symbol: "ETHUSDT", risk: {status: "MISSING_SIZING_EVIDENCE"}}], limitations: []}); await tick();
    assert(get("review-risk-summary").textContent.includes("0.35 R"));
    assert(get("review-risk-summary").textContent.includes("有效 2 笔 / 缺失 1 笔"));
    assert(get("review-risk-summary").textContent.includes("不是盈利预测"));
    assert(get("review-risk-groups").textContent.includes("<img onerror=bad>"));
    assert(get("review-trades").textContent.includes("10 / 0.35 R / 0%"));
    assert(get("review-trades").textContent.includes("无法计算"));
    get("account").value = "B"; fire("account", "change"); await tick();
    assert.equal(get("review-risk-summary").textContent, ""); assert.equal(get("review-risk-groups").children.length, 0);
    assert(requests.every(r => r.args.method === "GET" && !r.args.body));
  },
  async readonly_review() {
    await boot();
    const response = (id = "A", environment = "SANDBOX") => ({registry_id: id, environment, status: "AVAILABLE", as_of: "2026-09-30T19:00:00+08:00", summary: {settled_trades: 1, net_pnl: "-2"}, comparisons: [{label: "<img onerror=bad>", kept_count: 1, excluded_count: 0, unknown_count: 2, known_baseline: {net_pnl: "-2"}, kept_actual: {net_pnl: "-2"}, excluded_actual: {net_pnl: "0"}}], trades: [], limitations: ["不是回测收益"]});
    fire("review-load"); await tick();
    assert(requests[4].path.endsWith("/strategy/review?days=14")); assert.equal(requests[4].args.method, "GET"); assert(!requests[4].args.body);
    get("account").value = "B"; fire("account", "change"); await tick();
    respond(5, policy("B")); respond(6, analytics("B")); await tick();
    respond(4, response()); await tick(); assert.equal(get("review-variants").children.length, 0);
    fire("review-load"); await tick(); respond(7, response("B")); await tick();
    assert(get("review-status").textContent.includes("Beta")); assert(get("review-variants").textContent.includes("<img onerror=bad>"));
    fire("review-load"); await tick(); respond(8, response("A")); await tick(); assert(get("review-status").textContent.includes("不匹配")); assert.equal(get("review-variants").children.length, 0);
    fire("review-load"); await tick(); respond(9, {registry_id: "B", environment: "SANDBOX", status: "BUSY"}); await tick(); assert(get("review-status").textContent.includes("正忙")); assert(!get("review-load").disabled);
    assert.equal(requests.length, 10);
  },
  async races_and_retry() {
    respond(0, {accounts}); await tick(); respond(1, {phase: "ACTIVE", target_registry: "A"}); await tick();
    get("account").value = "B"; fire("account", "change"); await tick();
    respond(4, policy("B")); respond(5, analytics("B", 22)); await tick();
    respond(2, policy("A")); respond(3, analytics("A", 999)); await tick();
    assert.equal(get("summary").children[2].children[1].textContent, "22");
    assert.equal(get("equity-total").textContent, "22"); assert.equal(get("equity-chart").children.length, 1);
    assert.equal(run("policy.registry_id"), "B");
    edit("10"); fire("preview"); await tick(); assert.deepEqual(JSON.parse(requests[6].args.body).changes, {"sizing.max_margin_fraction": "0.1"});
    edit("15"); respond(6, preview("B")); await tick(); assert.equal(run("pending"), null); assert(get("apply").disabled);
    fire("preview"); await tick(); respond(7, preview("B", "0.15")); await tick();
    assert(get("apply").disabled, "Reason is required"); get("reason").value = "QA"; fire("reason", "input"); assert(!get("apply").disabled);
    fire("apply"); await tick(); const first = JSON.parse(requests[8].args.body);
    assert.equal(requests[8].args.headers["X-V2-Action"], "strategy-management");
    respond(8, {error: "unavailable"}, false); await tick();
    assert(!get("apply").disabled); assert(get("preview").disabled); assert(run("inputs.get('sizing.max_margin_fraction').disabled"));
    assert(get("reason").disabled); get("reason").value = "changed after uncertain response";
    fire("apply"); await tick(); assert.deepEqual(JSON.parse(requests[9].args.body), first);
    get("environment").value = "LIVE"; fire("environment", "change"); await tick();
    respond(10, policy("C", "LIVE")); respond(11, analytics("C", 0, "LIVE")); await tick(); respond(9, {version: 2}); await tick();
    assert.equal(run("policy.registry_id"), "C"); assert(get("apply").disabled); assert(get("preview").disabled);
    assert(!get("apply-status").textContent.includes("v2")); assert.equal(get("equity-total").textContent, "—");
    assert(get("equity-chart").textContent.includes("暂无有效净值采样")); assert(!get("preview-context").textContent.includes("Beta"));
    assert.equal(requests.length, 12);
  },
  async percentages() {
    const p = policy(); p.values["sizing.cost_buffer_fraction"] = "0.00123456789123456789"; await boot(p);
    assert.equal(run("inputs.get('sizing.max_margin_fraction').value"), "5"); assert.equal(run("inputs.get('capital.pool_fraction').value"), "70");
    assert.deepEqual(clone(run("draft().changes")), {}); assert(get("preview").disabled);
    for (const raw of ["0", "0.00000001", "0.05000", ".005", "0.00123456789123456789", "001.10"]) assert.equal(run(`rawValue('sizing.risk_fraction', displayValue('sizing.risk_fraction', '${raw}'))`), run(`decimalShift('${raw}')`));
    for (const bad of ["5%", "-1", "1e2", "NaN", "", "."]) { edit(bad); assert(get("preview").disabled); assert.equal(run("inputs.get('sizing.max_margin_fraction').attributes['aria-invalid']"), "true"); }
    edit("5"); edit("65", "capital.pool_fraction");
    assert.deepEqual(clone(run("draft().changes")), {"capital.pool_fraction": "0.65", "sizing.pool_fraction": "0.65"});
    assert(get("reserve-label").textContent.includes("35%")); edit("71", "capital.pool_fraction"); assert(get("preview").disabled);
    edit("65", "capital.pool_fraction"); edit("66"); assert(get("preview").disabled); edit("5");
    edit("4", "sizing.risk_fraction"); assert(get("preview").disabled); edit("0.5", "sizing.risk_fraction");
    edit("25", "sizing.min_allocation"); assert(get("preview").disabled); fire("discard");
    assert.deepEqual(clone(run("draft().changes")), {}); assert(get("preview").disabled); assert.equal(requests.length, 4);
    edit("6"); allowConfirm = false; get("account").value = "B"; fire("account", "change");
    assert.equal(get("account").value, "A"); assert.equal(requests.length, 4); assert.equal(run("inputs.get('sizing.max_margin_fraction').value"), "6");
    let prevented = false; events.beforeunload({preventDefault() { prevented = true; }}); assert(prevented);
  },
  async tabs_and_trades() {
    const a = analytics("A", 14); a.summary.net_pnl = "-6.59";
    a.trades = Array.from({length: 45}, (_, i) => ({id: String(i), symbol: `COIN${i}USDT`, side: "BUY", source: i % 2 ? "tv_bridge" : "s3", net_pnl: i === 0 ? null : String(i % 2 ? i : -i), fees_usdt: "0.1", held_minutes: i, maintenance: false}));
    a.receipts = [{consumer: '["BINANCE","A","SANDBOX","FUTURES","s6"]', source: "tv_bridge", reason: "score_below_minimum", outcome: "REJECT", receipts: 3}]; await boot(policy(), a);
    assert.equal(get("summary").children[0].children[1].className, "negative"); assert(get("receipts").textContent.includes("多头策略")); assert(get("receipts").textContent.includes("综合评分未达标"));
    fire("tab-risk"); edit("6"); fire("tab-trades"); assert.equal(run("inputs.get('sizing.max_margin_fraction').value"), "6");
    fire("tab-trades", "keydown", {key: "ArrowRight", preventDefault() {}}); assert.equal(run("currentTab"), "signals"); assert(get("tab-signals").focused);
    assert(get("trade-page-info").textContent.includes("1 / 3")); assert.equal(get("trades").children[0].children[1].children.length, 20);
    fire("trade-next"); fire("trade-next"); assert.equal(get("trades").children[0].children[1].children.length, 5); assert(get("trade-next").disabled);
    get("trade-search").value = "coin1usdt"; fire("trade-search", "input"); assert(get("trade-page-info").textContent.includes("1 条匹配"));
    get("trades").children[0].children[1].children[0].children[6].children[0].listeners.click(); assert(get("trade-dialog").open); assert(get("trade-dialog-title").textContent.includes("COIN1USDT")); fire("close-trade"); assert(!get("trade-dialog").open);
    get("trade-result").value = "loss"; fire("trade-result", "change"); assert(get("trades").textContent.includes("没有符合筛选")); fire("reset-filters");
    get("trade-source").value = "tv_bridge"; fire("trade-source", "change"); assert(get("trade-page-info").textContent.includes("22 条匹配"));
    get("trade-sort").value = "profit"; fire("trade-sort", "change"); assert(get("trades").children[0].children[1].children[0].textContent.includes("COIN43USDT"));
    get("chart-point").value = "0"; fire("chart-point", "input"); assert(get("chart-readout").textContent.includes("净值 1 U"));
    assert(get("equity-cash").textContent.includes("<img onerror=bad>")); assert.equal(requests.length, 4);
  },
  async load_failures() {
    respond(0, {error: "offline"}, false); await tick(); assert(!get("error-banner").hidden); fire("retry"); await tick();
    assert.equal(requests[1].path, "/api/accounts"); respond(1, {accounts}); await tick(); respond(2, {}); await tick(); respond(3, policy()); respond(4, analytics("A", 22)); await tick();
    fire("refresh"); await tick(); respond(5, policy()); respond(6, {error: "offline"}, false); await tick();
    assert(!get("error-banner").hidden); assert.equal(get("summary").children.length, 0); assert.equal(get("equity-total").textContent, "—");
    assert.equal(get("trade-scope").textContent, ""); assert(!get("refresh").disabled);
    fire("retry"); await tick(); respond(7, policy()); respond(8, analytics()); await tick(); assert(get("error-banner").hidden);
    run("accounts = accounts.filter(a => a.environment !== 'LIVE')"); get("environment").value = "LIVE"; fire("environment", "change"); await tick();
    assert(get("status").textContent.includes("没有可选账户")); assert(!get("refresh").disabled); assert.equal(requests.length, 9);
  },
  async expiry_and_restore() {
    const p = policy("A", "SANDBOX", 2); p.history = [{version: 1, at: "2026-09-28", reason: "history"}]; await boot(p);
    fire("restore"); await tick(); assert.deepEqual(JSON.parse(requests[4].args.body), {expected_version: 2, changes: {}, restore_version: 1});
    const expired = preview("A", "0.1", 2); expired.preview_expires_at_ms = Date.now() - 10; respond(4, expired); await tick();
    get("reason").value = "QA"; fire("reason", "input"); assert(get("apply").disabled); assert(get("preview-expiry").textContent.includes("已过期")); fire("apply"); assert.equal(requests.length, 5);
    fire("discard"); edit("10"); fire("preview"); await tick(); const wrong = preview("B", "0.1", 2); respond(5, wrong); await tick(); assert.equal(run("pending"), null); assert(get("preview-result").textContent.includes("不匹配"));
  },
  async readonly_legacy() {
    await boot();
    get("environment").value = "LIVE"; fire("environment", "change"); await tick();
    const legacy = policy("C", "LIVE"); legacy.values["sizing.max_margin_fraction"] = "1"; legacy.values["sizing.pool_fraction"] = "0.80";
    respond(4, legacy); respond(5, analytics("C", 0, "LIVE")); await tick();
    assert.equal(run("dirty"), false); assert(get("dirty-dot").hidden); assert(get("pool-config-note").textContent.includes("80%"));
    let prevented = false; events.beforeunload({preventDefault() { prevented = true; }}); assert(!prevented);
    allowConfirm = false; get("environment").value = "SANDBOX"; fire("environment", "change"); await tick();
    assert.equal(get("environment").value, "SANDBOX"); assert.equal(requests.length, 8);
    respond(6, policy()); respond(7, analytics()); await tick(); assert.equal(get("pool-config-note").textContent, "");
  },
  async account_mismatch() {
    await boot(policy("B"), analytics("A", 999)); assert.equal(run("policy"), null); assert.equal(get("summary").children.length, 0); assert(!get("error-banner").hidden);
    assert(get("error-message").textContent.includes("账户响应不匹配")); fire("retry"); await tick(); respond(4, policy()); respond(5, analytics()); await tick(); assert.equal(run("policy.registry_id"), "A");
  }
};
scenarios[process.argv[3]]().then(() => console.log(`PASS ${process.argv[3]}`)).catch(e => { console.error(e); process.exitCode = 1; });
