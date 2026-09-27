"use strict";
const $ = id => document.getElementById(id);
let accounts = [], generation = 0, routeEpoch = null;
const notice = text => { $("notice").textContent = text; };
async function api(path, body) {
  const response = await fetch(path, {method: body ? "POST" : "GET", credentials: "same-origin", cache: "no-store", headers: body ? {"Content-Type":"application/json", "X-V2-Action":"account-management"} : {}, ...(body ? {body:JSON.stringify(body)} : {})});
  if (!response.ok) throw new Error("操作未完成，请刷新核对状态；请勿在反馈中附带密钥。");
  return response.json();
}
async function loadAccounts(selected) {
  const result = await api("/api/accounts"); accounts = result.accounts;
  const selection = accounts.find(a=>a.registry_id === selected);
  if (selection) $("environment").value = selection.environment;
  await filterAccounts(selected);
}
async function filterAccounts(selected) {
  const environment = $("environment").value || "SANDBOX";
  for (const id of ["account"]) {
    $(id).replaceChildren(...accounts.filter(a=>a.environment === environment).map(a => new Option(`${a.alias} · ${a.environment}`, a.registry_id)));
  }
  if (selected && accounts.some(a=>a.registry_id === selected && a.environment === environment)) $("account").value = selected;
  await showAccount();
}
function verificationText(result) {
  const status = {NOT_CHECKED:"尚未验证",SIGNED_READ_ACCEPTED:"签名只读验证通过",SIGNED_READ_FAILED:"签名只读验证失败"}[result.outcome] || "状态未知";
  const d=result.diagnostic || {};
  return `${status}${result.checked_at ? " · "+result.checked_at : ""}${result.checked_at && !result.fresh ? " · 结果已过期，需重新验证" : ""}${d.http_status ? " · HTTP "+d.http_status : ""}${d.exchange_code ? " · 币安错误码 "+d.exchange_code : ""}${d.exchange_code === -2015 ? "（请检查是否为期货 Testnet Key、Key/Secret 配对、IP 白名单和 API 权限；具体原因尚未确定）" : ""}${d.category ? " · "+d.category : ""}${result.cached ? " · 冷却期内复用最近结果" : ""} · 不授权交易`;
}
async function showVerification(id, version) {
  try {
    const result=await api(`/api/accounts/${id}/verification`);
    if(version===generation && $("account").value===id) $("verification").textContent=verificationText(result);
  } catch(e) { if(version===generation) $("verification").textContent="暂时无法读取验证记录；请重试。"; }
}
async function showAccount() {
  const version = ++generation, id = $("account").value;
  $("summary").replaceChildren(); $("trades").replaceChildren(); $("state").textContent = "";
  const account = accounts.find(a=>a.registry_id === id);
  routeEpoch=null; $("route-request").disabled=true; $("route-state").textContent="正在读取执行状态…";
  $("rotate-submit").disabled = !account;
  $("rotation-target").textContent = account ? `修改目标：${account.alias} · ${account.environment} · 凭据版本 ${account.binding_version}` : "请先选择账户";
  if ($("rotate").elements) { $("rotate").elements.api_key.value=""; $("rotate").elements.api_secret.value=""; }
  $("verify").disabled = !account || account.environment !== "SANDBOX";
  $("verification").textContent = account?.environment === "LIVE" ? "生产账户仅加密保存，验证与交易均未启用。" : "正在读取验证记录…";
  if (!account) { $("state").textContent = "尚无已登记账户"; $("verification").textContent="请选择账户"; showRoute($("environment").value || "SANDBOX", version); return; }
  $("alias").value = account.alias;
  $("state").textContent = `${account.status} · ${account.encrypted_credential_present ? "凭据已加密保存" : "尚无加密凭据"} · 未授权交易`;
  try {
    const result = await api(`/api/accounts/${id}/overview`);
    if (version !== generation || $("account").value !== result.registry_id) return;
    for (const [key, label] of [["trade_intents","交易意图"],["settled_trades","已结算交易"],["settled_net_pnl_usdt","已结算净收益 USDT"]]) {
      const p = document.createElement("p"), strong = document.createElement("strong"); p.textContent = label; strong.textContent = result.summary[key]; p.append(strong); $("summary").append(p);
    }
    for (const trade of result.trades) { const p=document.createElement("p"); p.textContent=`${trade.symbol} · ${trade.status} · ${trade.created_at} · 净收益 ${trade.net_pnl_usdt ?? "未结算"}`; $("trades").append(p); }
    if(account.environment === "SANDBOX") await showVerification(id, version);
    if(version===generation) await showRoute(account.environment, version);
  } catch (e) { if(version === generation) notice(e.message); }
}
$("account").addEventListener("change", showAccount);
$("environment").addEventListener("change", ()=>filterAccounts());
$("verify").addEventListener("click", async ()=>{
  const account=accounts.find(a=>a.registry_id === $("account").value), version=generation;
  if(!account || account.environment!=="SANDBOX") return;
  $("verify").disabled=true; $("verification").textContent="正在执行 Testnet 签名只读验证…";
  try {
    const result=await api(`/api/accounts/${account.registry_id}/verification`, {expected_version:account.binding_version});
    if(version===generation) $("verification").textContent=verificationText(result);
  } catch(e) { if(version===generation) $("verification").textContent="验证未完成，可能正在验证、凭据版本已变化或服务不可用；请刷新后重试。"; }
  finally { if(version===generation) $("verify").disabled=false; }
});
$("add").addEventListener("submit", async event => {
  event.preventDefault(); const form=event.currentTarget, button=form.querySelector("button"); button.disabled=true;
  const body=Object.fromEntries(new FormData(form)); body.request_id=crypto.randomUUID();
  form.elements.api_key.value=""; form.elements.api_secret.value="";
  try { const result=await api("/api/accounts",body); notice("加密登记成功；尚未激活交易。"); await loadAccounts(result.registry_id); }
  catch(e) { notice(e.message); }
  finally { body.api_key=""; body.api_secret=""; button.disabled=false; }
});
$("rename").addEventListener("submit", async event => {
  event.preventDefault(); const a=accounts.find(x=>x.registry_id === $("account").value); if(!a)return;
  try { await api(`/api/accounts/${a.registry_id}/alias`,{alias:$("alias").value,expected_version:a.alias_version,request_id:crypto.randomUUID()}); await loadAccounts(a.registry_id); notice("别名已更新，账户历史身份不变。"); } catch(e) {notice(e.message);}
});
$("rotate").addEventListener("submit",async event=>{
  event.preventDefault();
  const account=accounts.find(a=>a.registry_id === $("account").value), version=generation;
  if(!account)return;
  const form=event.currentTarget, button=$("rotate-submit"), body=Object.fromEntries(new FormData(form));
  body.request_id=crypto.randomUUID(); body.expected_version=account.binding_version;
  form.elements.api_key.value=""; form.elements.api_secret.value=""; button.disabled=true;
  try {
    const result=await api(`/api/accounts/${account.registry_id}/credentials`,body);
    if(version===generation) { notice("新凭据已加密保存，历史统计保留；请重新验证。交易未启动。"); await loadAccounts(account.registry_id); }
    else { account.binding_version=result.binding_version; }
  }catch(e){if(version===generation)notice(e.message);}
  finally {body.api_key="";body.api_secret="";if(version===generation)button.disabled=false;}
});
function routeText(result) {
  const phases={STOPPED:"无已确认执行者",BLOCKED:"申请被阻断，尚未切换",REQUESTED:"等待执行控制器处理",STARTING:"等待新进程确认就绪",ACTIVE:"执行者已确认",DRAINING:"旧账户退出中：仅管理已有仓位"};
  const reasons={EXECUTION_CONTROLLER_NOT_ATTACHED:"等待独立控制器处理",LIVE_DEPLOYMENT_NOT_APPROVED:"生产隔离部署与上线验收尚未完成",TARGET_CREDENTIAL_NOT_VERIFIED:"目标凭据尚未验证通过",ENTRY_DISABLED_ACCEPTANCE:"切换验收模式：新开仓关闭",RECOVERING_SOURCE:"恢复旧账户，仅管理已有仓位",TARGET_PREFLIGHT_FAILED:"目标账户就绪检查失败",SOURCE_PROCESS_NOT_STOPPED:"旧进程尚未确认停止",ACCOUNT_LEDGER_NOT_CLEAR:"账户仍有未结业务"};
  const target=accounts.find(a=>a.registry_id===result.target_registry);
  return `${phases[result.phase] || "未知状态"}${target ? " · 账户 "+target.alias : ""} · 版本 ${result.epoch}${result.pending_request ? " · 有新切换申请等待处理" : ""}${result.blockers?.length ? " · "+result.blockers.map(x=>reasons[x]||x).join("；") : ""}`;
}
async function showRoute(environment, version) {
  try {
    const result=await api(`/api/accounts/execution/${environment}`);
    if(version!==generation)return;
    routeEpoch=result.request_epoch ?? result.epoch; $("route-state").textContent=routeText(result);
    $("route-request").disabled=!accounts.some(a=>a.registry_id===$("account").value);
  }catch(e){if(version===generation)$("route-state").textContent="执行状态不可用；禁止申请切换。";}
}
$("route-request").addEventListener("click",async()=>{
  const account=accounts.find(a=>a.registry_id===$("account").value), version=generation;
  if(!account || routeEpoch===null)return;
  $("route-request").disabled=true;
  try {
    const result=await api(`/api/accounts/execution/${account.environment}`,{target_registry:account.registry_id,binding_version:account.binding_version,expected_epoch:routeEpoch,request_id:crypto.randomUUID()});
    if(version===generation){routeEpoch=result.epoch;$("route-state").textContent=routeText(result);}
  }catch(e){if(version===generation)$("route-state").textContent="申请未完成，请刷新核对状态；未授权自动切换。";}
  finally {if(version===generation)$("route-request").disabled=false;}
});
loadAccounts().catch(e=>notice(e.message));
if(typeof setInterval === "function") setInterval(()=>{
  if(typeof document.hidden === "boolean" && document.hidden)return;
  showRoute($("environment").value || "SANDBOX",generation);
},5000);
