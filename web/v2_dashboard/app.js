const state = { data: null, filter: "all", search: "", detailGeneration: 0, page: 1 };
const $ = (id) => document.getElementById(id);
const workspace = window.Atlas;
const showTab = workspace?.tabs(["overview", "trades", "signals", "health"], "overview") || (() => {});
const esc = (value) => String(value ?? "—").replace(/[&<>"']/g, (x) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[x]));
const num = (value, digits=2) => value == null ? "—" : new Intl.NumberFormat("zh-CN", {maximumFractionDigits:digits}).format(Number(value));
const money = (value) => value == null ? "—" : `${Number(value) >= 0 ? "+" : ""}${num(value,2)}`;
const at = (value) => value ? new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(value)) : "—";
const atMs = (value) => value ? at(Number(value)) : "—";
const held = (trade) => Number(trade.open_qty) > Number(trade.close_qty);
const sourceName = (source) => ({tv_bridge:"TradingView",s3:"S3",quant:"QUANT · 量化候选"}[source] || source || "未知来源");
const status = (trade) => trade.settled_at ? "已结算" : held(trade) ? "持仓中" : trade.status === "CANCELLED" ? "已取消" : trade.status === "REJECTED" ? "已拒绝" : "处理中";
function renderMetrics(data) {
  const account=data.execution_account;
  $("account-alias").textContent=account?.alias || '未确认当前账户';
  workspace?.describe(account?.alias, "SANDBOX", "执行");
  if(workspace && !workspace.requested.account && workspace.requested.environment === "SANDBOX") workspace.setContext({account:account?.registry_id || "",environment:"SANDBOX"});
  $("view-context-note").hidden = !workspace || (!workspace.requested.account && workspace.requested.environment === "SANDBOX") || (workspace.requested.account === account?.registry_id && workspace.requested.environment === "SANDBOX");
  const phaseLabel = {ACTIVE:'执行已确认',DRAINING:'正在退出旧仓位',BLOCKED:'执行阻塞，未确认交易运行',STARTING:'正在启动，尚未就绪',STOPPED:'执行已停止'};
  $("execution-account").textContent=account ? `当前路由账户：${account.alias} · Testnet · ${phaseLabel[account.phase] || '执行状态未知'}${account.blockers?.length ? ' · 阻塞/限制：' + account.blockers.join('、') : ''}` : '执行账户未知';
  const s = data.summary;
  $("pnl").textContent = money(s.realized_pnl);
  $("pnl").className = "metric-main " + (Number(s.realized_pnl) >= 0 ? "positive" : "negative");
  $("open").textContent = s.open_positions;
  $("intents").textContent = s.trade_intents;
  $("winrate").textContent = s.win_rate == null ? "—" : s.win_rate + "%";
  $("settled").textContent = s.settled_trades + " 笔已结算交易";
  $("asof").textContent = "更新于 " + at(data.as_of) + " · UTC+8";
}
function renderChart(data) {
  const values = data.settlements.map((x) => Number(x.net_pnl || 0));
  let cumulative = 0;
  const points = [0,...values.map((x) => cumulative += x)];
  const lo = Math.min(...points), hi = Math.max(...points);
  const span = Math.max(hi-lo, Math.abs(hi)*.15, 1);
  const pts = points.map((v,i) => [i/(points.length-1 || 1)*600, 155-(v-lo)/span*130]);
  const path = pts.map(([x,y],i) => (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1)).join(" ");
  $("chart-line").setAttribute("d",values.length ? path : "");
  $("chart-area").setAttribute("d",values.length ? path + " L 600 180 L 0 180 Z" : "");
  $("chart-empty").hidden = values.length > 0;
  $("chart-value").textContent = money(data.summary.realized_pnl) + " USDT";
  $("chart-value").className = "chart-value " + (Number(data.summary.realized_pnl) >= 0 ? "positive" : "negative");
}
function renderAccounting(data) {
  const daily = data.daily || {};
  $("daily-report").innerHTML = `<div class="detail-row">${esc(daily.note)}</div>` +
    (daily.cash || []).slice(0,14).map(row => `<div class="detail-row"><strong>${esc(row.day)} · ${esc(row.account_id)}</strong><br>当日实际收支：${money(row.net_pnl)} USDT${row.unvalued_events ? " · 存在未计价事件，数值未知" : ""}</div>`).join("") +
    (daily.closed_trades || []).slice(0,14).map(row => `<div class="detail-row">${esc(row.day)} 完整交易业绩：${money(row.net_pnl)} USDT · ${num(row.trades,0)} 笔</div>`).join("") +
    '<div class="detail-row">每日净值口径：历史快照不足时不推算，不用收支代替浮盈亏。</div>';
  renderFunnel(data);
}
function renderFunnel(data) {
  const status = data.signal_funnel_status;
  $("signal-funnel").innerHTML = status === "DEFERRED" ? '<div class="placeholder">信号统计独立加载中…</div>' : status === "TIMEOUT" || status === "UNAVAILABLE" ? '<div class="placeholder">信号统计暂不可用，等待下次刷新；不影响已展示的交易与持仓数据。</div>' : (data.signal_funnel || []).map(row => `<div class="detail-row"><strong>${esc(sourceName(row.source))}</strong><br>收到 ${num(row.received,0)} → 已处理 ${num(row.processed,0)} → 形成意图 ${num(row.intents,0)} → 开仓成交 ${num(row.filled,0)}</div>`).join("") || '<div class="placeholder">今日尚未收到信号</div>';
}
async function refreshFunnel(snapshot) {
  state.funnelController?.abort();
  const controller = new AbortController();
  state.funnelController = controller;
  const timeout = setTimeout(() => controller.abort(), 7000);
  try {
    const response = await fetch('/api/signal-funnel', {cache:'no-store', signal:controller.signal});
    if (!response.ok) throw new Error('funnel unavailable');
    const result = await response.json();
    if (state.data !== snapshot) return;
    const expected = snapshot.execution_account, actual = result.execution_account;
    if (!expected || !actual || expected.registry_id !== actual.registry_id || expected.epoch !== actual.epoch) {
      renderFunnel({signal_funnel_status:'UNAVAILABLE'});
      return;
    }
    renderFunnel(result);
  } catch {
    if (state.data === snapshot) renderFunnel({signal_funnel_status:'UNAVAILABLE'});
  } finally {
    clearTimeout(timeout);
    if (state.funnelController === controller) state.funnelController = null;
  }
}
function renderPositions(data) {
  const open = data.trades.filter(held);
  $("position-count").textContent = open.length + " 个仓位";
  $("position-list").innerHTML = open.length ? open.map((t) => `<div class="position"><div class="position-left"><strong>${esc(t.symbol)} <span class="side ${t.side === "BUY" ? "long":"short"}">${t.side === "BUY" ? "LONG":"SHORT"}</span></strong><small>${esc(t.producer?.toUpperCase())} · ${esc(t.event_type || t.rationale || "方向策略")}</small></div><div class="position-right"><strong>${num(Number(t.open_qty)-Number(t.close_qty),8)}</strong><small>开仓均价 ${num(t.entry_price,8)}</small></div></div>`).join("") : '<div class="placeholder">当前没有账本未平仓位</div>';
}
function renderTrades(data) {
  let rows = data.trades;
  if (state.filter === "open") rows = rows.filter(held);
  if (state.filter === "settled") rows = rows.filter((x) => x.settled_at);
  if (state.search) rows = rows.filter((x) => (x.symbol || "").toLowerCase().includes(state.search));
  const pages=Math.max(1,Math.ceil(rows.length/20));state.page=Math.min(pages,Math.max(1,state.page));
  $("trade-count").textContent = `${rows.length} 笔匹配 · 第 ${state.page} / ${pages} 页`;
  $("trade-prev").disabled=state.page<=1;$("trade-next").disabled=state.page>=pages;
  $("trade-rows").innerHTML = rows.length ? rows.slice((state.page-1)*20,state.page*20).map((t) => {
    const label = status(t), pnl = t.settled_net_pnl;
    return `<tr data-id="${esc(t.id)}"><td class="symbol-cell"><strong>${esc(t.symbol)}</strong><small>${esc(t.producer?.toUpperCase())} · ${esc(t.event_type || t.signal_source || "方向策略")}</small></td><td><span class="side ${t.side === "BUY" ? "long":"short"}">${t.side === "BUY" ? "做多":"做空"}</span></td><td><span class="badge ${t.settled_at ? "done":held(t) ? "wait":""}">${label}</span></td><td>${num(t.entry_price,8)}</td><td>${num(t.open_qty,8)}</td><td class="${Number(pnl) >= 0 ? "positive":"negative"}">${pnl == null ? "—" : money(pnl)}</td><td>${at(t.created_at)}</td><td><button class="text-button" aria-label="查看 ${esc(t.symbol)} 交易详情">详情 →</button></td></tr>`;
  }).join("") : '<tr><td colspan="8" class="empty-cell">没有符合条件的交易</td></tr>';
}
function renderSignals(data) {
  $("signal-list").innerHTML = data.signals.length ? data.signals.map((s) => `<div class="signal"><div class="signal-top"><strong>${esc(s.symbol)}</strong><span class="signal-source">${esc(sourceName(s.source))}</span></div><div class="signal-event">${esc(s.signal)}</div><div class="signal-time">触发 ${atMs(s.observed_at_ms)} · 收到 ${at(s.received_at)}${s.strength ? " · 强度 "+esc(s.strength):""}</div><div class="signal-time">${(s.decisions || []).map(d=>esc(d.outcome)+" · "+esc(d.reason)).join("<br>") || "等待处理"}</div></div>`).join("") : '<div class="placeholder">暂无近期信号</div>';
}
function renderHealth(data) {
  const health=data.health || [], stale=health.some(x=>!Number.isFinite(x.payload?.observed_at_ms) || Date.now()-x.payload.observed_at_ms>60000 || x.payload.observed_at_ms>Date.now()+5000);
  const affected=health.some(x=>(x.payload?.active || []).length || Object.keys(x.payload?.findings || {}).length);
  $("health-status").textContent=!health.length || stale ? "链路检查未知 / 数据过期" : affected ? "链路检查有待处理事项" : "本次链路检查未发现超时异常";
  $("health-status").className=!health.length || stale || affected ? "negative" : "";
  const labels = {ORDER_PROGRESS_STALLED:"订单推进超时",SETTLEMENT_OVERDUE:"平仓后结算超时",SIGNAL_CONSUMPTION_LAG:"信号消费延迟",PIPELINE_ENTRY_BLOCKED:"开仓链路持续阻塞",POSITION_SAFETY_BLOCKED:"保护 / 退出环节阻塞",CAPITAL_DRAWDOWN_HALT:"累计风控硬停，禁止自动解锁"};
  $("health-list").innerHTML = (data.health || []).length ? data.health.map(({account_id,payload:h}) => {
    const stale = !Number.isFinite(h.observed_at_ms) || Date.now()-h.observed_at_ms > 60000 || h.observed_at_ms > Date.now()+5000;
    const active = h.active || [], findings = h.findings || {};
    const title = stale ? "检查数据过期 / 状态未知" : active.length ? "需要处理："+active.map(x=>labels[x] || x).join("、") : Object.keys(findings).length ? "发现异常，正在持续性确认" : "本次检查未发现超时异常";
    const rows = Object.entries(findings).map(([code,detail]) => `<details class="detail-row"><summary>${esc(labels[code] || code)}</summary><pre>${esc(JSON.stringify(detail,null,2))}</pre></details>`).join("") +
      (Object.keys(h.expected_waits || {}).length ? `<details class="detail-row"><summary>正常风控等待：冷却、容量或已有仓位</summary><pre>${esc(JSON.stringify(h.expected_waits,null,2))}</pre></details>` : "") +
      (h.capital ? `<div class="detail-row">资金风险状态：${esc(h.capital.recovery?.mode || "ACTIVE")} · 试运行次数 ${num(h.capital.recovery?.attempts || 0,0)} · 风险系数 ${esc(h.capital.factor)}</div>` : "");
    return `<div class="detail-row"><strong class="${stale || active.length ? "negative" : ""}">${esc(title)}</strong><div>${esc(account_id)} · 检查于 ${atMs(h.observed_at_ms)} · UTC+8</div>${rows}</div>`;
  }).join("") : '<div class="placeholder">业务监测尚未产生结果，不能据此认定交易健康。</div>';
}
function detailItem(label,value) { return `<div class="detail-item"><label>${esc(label)}</label><span>${esc(value)}</span></div>`; }
function detailSection(title,body) { return `<section class="detail-section"><h3>${esc(title)}</h3>${body}</section>`; }
async function openTrade(id) {
  const generation=++state.detailGeneration;
  if(!$("drawer").open) $("drawer").showModal();
  $("drawer").setAttribute("aria-hidden","false");
  $("drawer-body").innerHTML = '<div class="placeholder">读取决策与成交证据...</div>';
  try {
    const response = await fetch("/api/trades/" + encodeURIComponent(id),{cache:"no-store"});
    if (!response.ok) throw new Error("detail unavailable");
    const t = await response.json(), d = t.decision || {}, p = d.market_plan || {}, z = d.sizing || {};
    if(generation!==state.detailGeneration)return;
    $("drawer-title").textContent = (d.symbol || "交易") + " · " + t.producer.toUpperCase();
    let html = detailSection("01 / 决策与开仓",'<div class="detail-grid">' +
      detailItem("方向",d.side === "BUY" ? "LONG / 买入" : "SHORT / 卖出") +
      detailItem("计划数量",d.planned_quantity) +
      detailItem("策略事件",p.event_type) +
      detailItem("策略分数",p.score) +
      detailItem("决策理由",d.rationale || p.reason) +
      detailItem("计划名义本金",z.notional) +
      detailItem("信号来源",t.signal_source || "S3 / 内部信号") +
      detailItem("信号时间",atMs(d.observed_at_ms)) + '</div>');
    html += detailSection("02 / 订单轨迹",t.orders.length ? t.orders.map((o) => `<div class="detail-row"><strong>${esc(o.leg)} · ${esc(o.status)}</strong><br>数量 ${num(o.quantity,8)} · 交易所订单 ${esc(o.exchange_order_id || "待确认")}<br>${at(o.updated_at)}</div>`).join("") : '<div class="detail-none">尚无订单记录</div>');
    html += detailSection("03 / 成交明细",t.fills.length ? t.fills.map((f) => `<div class="detail-row"><strong>${esc(f.leg)} · ${num(f.quantity,8)} @ ${num(f.price,8)}</strong><br>手续费 ${num(f.fee,8)} ${esc(f.fee_currency)} · ${atMs(f.occurred_at_ms)}</div>`).join("") : '<div class="detail-none">尚无成交记录</div>');
    html += detailSection("04 / 平仓与结算",t.settlement ? '<div class="detail-grid">' + detailItem("净盈亏",money(t.settlement.net_pnl) + " " + t.settlement.currency) + detailItem("结算时间",at(t.settlement.settled_at)) + detailItem("收益率",t.outcome ? num(t.outcome.return_pct,2)+"%" : "未生成复盘") + detailItem("平仓价格",t.outcome ? num(t.outcome.closing_price,8) : "—") + '</div>' : '<div class="detail-none">尚未结算，不能把浮动盈亏当作已实现收益。</div>');
    html += detailSection("05 / 追溯",'<div class="detail-grid">' + detailItem("交易 ID",t.id) + detailItem("原始信号 ID",t.signal_event_id || "内部信号") + '</div>');
    $("drawer-body").innerHTML = html;
  } catch {
    if(generation!==state.detailGeneration)return;
    $("drawer-body").innerHTML = '<div class="placeholder">交易详情暂不可用，请稍后重试。</div>';
  }
}
function closeTrade() { ++state.detailGeneration; $("drawer-body").replaceChildren(); if($("drawer").open) $("drawer").close(); $("drawer").setAttribute("aria-hidden","true"); $("drawer-shade").hidden=true; }
async function refresh() {
  if(state.refreshing)return;
  state.refreshing=true;
  $("refresh").disabled=true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 7000);
  try {
    const response = await fetch("/api/overview",{cache:"no-store",signal:controller.signal});
    if (!response.ok) throw new Error("unavailable");
    const next=await response.json();
    if(state.data?.execution_account?.epoch!==next.execution_account?.epoch) closeTrade();
    state.data = next;
    $("error").hidden=true;
    renderMetrics(state.data); renderAccounting(state.data); renderChart(state.data); renderPositions(state.data); renderTrades(state.data); renderSignals(state.data); renderHealth(state.data);
    void refreshFunnel(next);
  } catch {
    state.funnelController?.abort();
    state.data=null; closeTrade();
    for(const id of ['pnl','open','intents','winrate','settled','asof','chart-value']) $(id).textContent='—';
    for(const id of ['daily-report','signal-funnel','position-list','trade-rows','signal-list','health-list']) $(id).replaceChildren();
    $("chart-line").setAttribute('d',''); $("chart-area").setAttribute('d','');
    $("position-count").textContent='未知'; $("trade-count").textContent='未知';
    $("execution-account").textContent='执行账户状态不可用，已隐藏旧统计';
    $("account-alias").textContent='当前账户未知';
    $("health-status").textContent='链路检查未知'; workspace?.describe(null,"SANDBOX","执行状态未知");
    $("trade-prev").disabled=true;$("trade-next").disabled=true;
    $("error").hidden=false;
  }
  finally { clearTimeout(timeout); state.refreshing=false; $("refresh").disabled=false; }
}
window.addEventListener('pageshow',refresh);
$("trade-rows").addEventListener("click",(event) => { const row=event.target.closest("tr[data-id]"); if(row) openTrade(row.dataset.id); });
document.querySelectorAll(".filter-tabs button").forEach((button) => button.addEventListener("click",() => { state.filter=button.dataset.filter;state.page=1; document.querySelectorAll(".filter-tabs button").forEach((x)=>x.classList.toggle("selected",x===button)); if(state.data) renderTrades(state.data); }));
$("search").addEventListener("input",(event)=>{state.page=1;state.search=event.target.value.trim().toLowerCase(); if(state.data) renderTrades(state.data);});
$("trade-prev").addEventListener("click",()=>{state.page--;if(state.data)renderTrades(state.data);});$("trade-next").addEventListener("click",()=>{state.page++;if(state.data)renderTrades(state.data);});
$("health-shortcut").addEventListener("click",()=>showTab("health",true));
$("drawer").addEventListener("cancel",closeTrade);
$("drawer-close").addEventListener("click",closeTrade); $("drawer-shade").addEventListener("click",closeTrade);
document.addEventListener("keydown",(event)=>{if(event.key==="Escape") closeTrade();});
$("refresh").addEventListener("click",refresh); $("retry").addEventListener("click",refresh);
function tick(){ $("clock").textContent=new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(new Date())+" CST"; }
tick(); setInterval(tick,1000); refresh(); setInterval(refresh,30000);
