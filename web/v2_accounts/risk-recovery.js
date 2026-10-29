"use strict";
(() => {
  const el = id => document.getElementById(id);
  let view = {id:"",alias:""}, serial = 0, preview = null, busy = false, pending = null;
  const labels = {
    TESTNET_ONLY:"生产账户不开放此恢复操作", NOT_CURRENT_EXECUTOR:"所选账户不是当前已确认的执行账户，或凭据已变更",
    ACCOUNT_SWITCH_PENDING:"存在执行账户切换申请，请先处理切换", CAPITAL_STATE_MISSING:"缺少账户资金或风控配置",
    RECOVERY_POLICY_UNSUPPORTED:"当前配置不支持受控恢复（需启用保护，试运行风险比例不超过 20%）",
    NOT_HALTED:"当前不是硬停状态，无需解除", HARD_HALT_REVIEW_REQUIRED:"不是自动试运行耗尽型硬停，需要另行复核",
    CAPITAL_SNAPSHOT_STALE:"账户资金快照已过期，等待执行器重新采集", CAPITAL_INVALID:"资金数据异常，禁止恢复",
    POLICY_SNAPSHOT_NOT_OBSERVED:"新参数尚未被资金采集确认，请稍后重新检查",
    HARD_DRAWDOWN_LIMIT:"仍触及累计最大回撤线，禁止从此处解除", RECOVERY_COOLDOWN:"仍在恢复冷却期，请等待提示时间后重新检查",
    ACCOUNT_NOT_FLAT:"账户有保证金占用或没有可用资金", COVERAGE_NOT_FRESH_CLEAR:"缺少最近两分钟内通过的交易所与台账对账",
    OTHER_HEALTH_BLOCKERS:"链路健康检查过期或存在其他阻塞", UNSETTLED_BUSINESS:"存在未结交易、非终态订单或未释放风险预留",
    RECOVERY_STATE_CHANGED:"账户、参数或硬停状态已改变，请重新检查后确认", RECOVERY_PREVIEW_EXPIRED:"确认信息已过期，请重新检查",
    RECOVERY_NOT_ELIGIBLE:"最新检查未通过，请刷新查看具体阻塞",
    RECOVERY_REQUEST_CONFLICT:"请求内容冲突，请刷新核对恢复记录", RECOVERY_REQUEST_INVALID:"恢复原因或请求格式不正确"
  };
  const time = ms => new Date(ms).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false})+" UTC+8";
  const number = n => Number(n).toLocaleString("zh-CN",{maximumFractionDigits:2});
  const percent = n => number(Number(n)*100)+"%";
  async function api(id, body) {
    const r = await fetch(`/api/accounts/${id}/risk-recovery`, {method:body?"POST":"GET",credentials:"same-origin",cache:"no-store",
      headers:body?{"Content-Type":"application/json","X-V2-Action":"account-management"}:{},...(body?{body:JSON.stringify(body)}:{})});
    const data = await r.json();
    if(!r.ok) throw new Error(labels[data.error] || "恢复服务不可用，请刷新核对；不会自动重试恢复。");
    return data;
  }
  function disable() { preview=null; el("risk-form").hidden=true; el("risk-apply").disabled=true; el("risk-ack").checked=false; }
  async function inspect() {
    const read=++serial, id=view.id;
    disable(); pending=null; el("risk-blockers").replaceChildren(); el("risk-history").replaceChildren(); el("risk-budget").textContent="";
    if(!id) { el("risk-status").textContent="请选择账户"; return; }
    el("risk-status").textContent="正在检查账户风控、执行归属和台账…";
    try {
      const data=await api(id);
      if(read!==serial || id!==view.id) return;
      const h=data.health, mode=h?.recovery?.mode;
      el("risk-status").textContent=`${view.alias} · ${({HALTED:"累计风控硬停",PROBE:"小风险试运行中",PAUSED:"自动恢复冷却中",ACTIVE:"风控运行中",OBSERVE_ONLY:"回撤仅记录（Testnet）"})[mode] || "不可恢复"}${h?` · 风控资金 ${number(h.base)} U · 历史高水位 ${number(h.peak)} U · 回撤 ${percent(h.drawdown)}`:""}`;
      for(const code of data.blockers || []) {const li=document.createElement("li");li.textContent=labels[code] || "检查未通过";el("risk-blockers").append(li);}
      if(h) el("risk-budget").textContent=`检查于 ${time(data.checked_at_ms)}。恢复风险比例为正常预算的 ${percent(data.probe_factor)}（不是保证金使用率）。试运行亏损 ${data.max_losses} 笔，或资金再下降 ${percent(data.loss_fraction)}，即再次硬停；累计回撤 ${percent(data.halt_drawdown)} 仍禁止开仓。至少 ${data.min_settlements} 笔已结算、合计盈利且回撤低于 ${percent(data.active_drawdown)}，才转回正常模式。${data.blockers?.includes("RECOVERY_COOLDOWN")?` 最早重新检查：${time(data.retry_at_ms)}。`:""}`;
      if(mode==="OBSERVE_ONLY") el("risk-budget").textContent=`检查于 ${time(data.checked_at_ms)}。本 Testnet 账户已暂停回撤缩仓、停机及小风险试运行限制；历史亏损与高水位继续记录。保证金使用上限、单笔止损和组合风险检查仍有效，生产账户不受影响。`;
      for(const row of data.history || []) {const p=document.createElement("p");p.textContent=`${time(row.at_ms)} · 已授权小风险试运行 · ${row.reason}`;el("risk-history").append(p);}
      if(data.eligible) { preview=data; el("risk-form").hidden=false; el("risk-apply").disabled=busy; }
    } catch(e) { if(read===serial) el("risk-status").textContent=e.message; }
  }
  window.addEventListener("atlas-account-view",event=>{
    view=event.detail; ++serial; pending=null; el("risk-reason").value=""; el("risk-result").textContent=""; void inspect();
  });
  el("risk-refresh").addEventListener("click",()=>{ if(!busy) void inspect(); });
  el("risk-form").addEventListener("submit",async event=>{
    event.preventDefault();
    const id=view.id, read=serial, p=preview, reason=el("risk-reason").value.trim();
    if(busy || !p || !p.eligible || !el("risk-ack").checked || reason.length<5 || reason.length>240) return;
    if(!pending && !window.confirm(`确认恢复“${view.alias}”的 Testnet 小风险试运行？风险比例 ${percent(p.probe_factor)}。会允许自动开新仓，历史亏损不清零；失败后再次硬停。`)) return;
    pending=pending || {request_id:crypto.randomUUID(),proof:p.proof,expires_at_ms:p.expires_at_ms,reason};
    const body=pending; busy=true; el("risk-apply").disabled=true;
    try {
      const result=await api(id,body);
      if(read!==serial || id!==view.id) return;
      el("risk-result").textContent=`已于 ${time(result.at_ms)} 授权一次小风险试运行。没有直接创建订单；后续仍须通过行情、信号和全部开仓检查。`;
      disable(); pending=null; await inspect();
    } catch(e) {
      if(read===serial && id===view.id) el("risk-result").textContent=e.message+" 如结果不明，可用同一请求重试，或刷新查看恢复记录。";
    } finally { busy=false; el("risk-apply").disabled=!preview?.eligible; }
  });
  // A cached account response may finish before this deferred asset loads.
  if(el("account").value) {
    view={id:el("account").value,alias:el("account").selectedOptions?.[0]?.textContent || "所选账户"};
    void inspect();
  }
})();
