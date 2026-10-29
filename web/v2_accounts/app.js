"use strict";
const $ = id => document.getElementById(id);
const workspace = typeof window !== "undefined" ? window.Atlas : null;
let accounts = [], generation = 0, routeEpoch = null, routeRead = 0, routeBusy = false;
let listRead = 0, viewing = {environment: "SANDBOX", account: ""};
const showTab = workspace?.tabs(["overview", "execution", "credentials"], "overview") || (() => {});
const notice = text => { $("notice").textContent = text; };
const amount = value => value === null || value === undefined || !Number.isFinite(Number(value)) ? "—" : Number(value).toLocaleString("zh-CN", {maximumFractionDigits:2});
async function api(path, body) {
  const response = await fetch(path, {method: body ? "POST" : "GET", credentials: "same-origin", cache: "no-store", headers: body ? {"Content-Type":"application/json", "X-V2-Action":"account-management"} : {}, ...(body ? {body:JSON.stringify(body)} : {})});
  if (!response.ok) throw new Error("操作未完成，请刷新核对状态；请勿在反馈中附带密钥。");
  return response.json();
}
async function loadAccounts(selected) {
  const serial = ++listRead;
  const result = await api("/api/accounts");
  if(serial !== listRead) return;
  accounts = result.accounts;
  if (!selected && ($("environment").value || "SANDBOX") === "SANDBOX") {
    try {
      const route=await api('/api/accounts/execution/SANDBOX');
      if(['ACTIVE','DRAINING'].includes(route.phase)) selected=route.target_registry;
    } catch { notice('无法确认当前执行账户；下拉框仅代表统计视图，请刷新执行状态。'); }
  }
  if(serial !== listRead) return;
  const selection = accounts.find(a=>a.registry_id === selected);
  if (selection) $("environment").value = selection.environment;
  await filterAccounts(selected);
  if(selected && !selection) notice("链接中的账户不可用，已显示当前可查看账户；请核对顶部选择。");
}
async function filterAccounts(selected) {
  const environment = $("environment").value || "SANDBOX";
  for (const id of ["account"]) {
    $(id).replaceChildren(...accounts.filter(a=>a.environment === environment).map(a => new Option(a.alias, a.registry_id)));
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
  if(typeof CustomEvent === "function") window.dispatchEvent(new CustomEvent("atlas-account-view", {detail:{id,alias:account?.alias || "",environment:account?.environment || ""}}));
  viewing = {account: id, environment: $("environment").value || "SANDBOX"};
  workspace?.setContext(viewing); workspace?.describe(account?.alias, viewing.environment);
  $("account-context").textContent = account ? `${account.alias} · 当前查看账户` : "该环境暂无已登记账户";
  $("alias").value = account?.alias || "";
  routeEpoch=null; $("route-request").disabled=true; $("route-state").textContent="正在读取执行状态…";
  $("route-target").textContent=account ? `目标账户：${account.alias} · ${account.environment}` : "请先选择目标账户";
  $("route-request").textContent=account?.environment === "LIVE" ? "生产执行暂未开放" : "切换执行账户";
  $("rotate-submit").disabled = !account;
  $("rotation-target").textContent = account ? `修改目标：${account.alias} · ${account.environment} · 凭据版本 ${account.binding_version}` : "请先选择账户";
  if ($("rotate").elements) { $("rotate").elements.api_key.value=""; $("rotate").elements.api_secret.value=""; }
  $("verify").disabled = !account || account.environment !== "SANDBOX";
  $("verification").textContent = account?.environment === "LIVE" ? "生产账户仅加密保存，验证与交易均未启用。" : "正在读取验证记录…";
  if (!account) { $("state").textContent = "尚无已登记账户"; $("verification").textContent="请选择账户"; showRoute($("environment").value || "SANDBOX", version); return; }
  $("alias").value = account.alias;
  $("state").textContent = `${({ACTIVE:"已登记",ENROLLED_UNVERIFIED:"已登记，验证结果另行检查",DISABLED:"已停用",RETIRED:"已归档"})[account.status] || account.status} · ${account.encrypted_credential_present ? "凭据已加密保存" : "尚无加密凭据"} · 实际执行状态见“验证与切换”`;
  // Execution controls must not depend on statistics or verification availability.
  void showRoute(account.environment, version);
  try {
    const result = await api(`/api/accounts/${id}/overview`);
    if (version !== generation || $("account").value !== result.registry_id) return;
    for (const [key, label] of [["trade_intents","交易意图"],["settled_trades","已结算交易"],["settled_net_pnl_usdt","已结算净收益 USDT"]]) {
      const p = document.createElement("p"), strong = document.createElement("strong"); p.className="metric"; p.textContent = label; strong.textContent = amount(result.summary[key]); if(key === "settled_net_pnl_usdt") { if(!Number(result.summary.settled_trades)) strong.textContent="—"; else strong.className=Number(result.summary[key])<0?"negative":Number(result.summary[key])>0?"positive":""; } p.append(strong); $("summary").append(p);
    }
    for (const trade of result.trades) { const p=document.createElement("p"); p.textContent=`${trade.symbol} · ${trade.status} · ${trade.created_at} · 净收益 ${trade.net_pnl_usdt === null ? "未结算" : amount(trade.net_pnl_usdt)} USDT`; $("trades").append(p); }
    if(account.environment === "SANDBOX") await showVerification(id, version);
  } catch (e) { if(version === generation) notice(e.message); }
}
function hasDraft() {
  const current=accounts.find(a=>a.registry_id===viewing.account);
  return (current && $("alias").value!==current.alias) || ["add","rotate"].some(id => {
    const form=$(id).elements;
    return form && (form.api_key?.value || form.api_secret?.value || (id==="add" && form.alias?.value));
  });
}
function discardAllowed() { return !hasDraft() || window.confirm("有尚未保存的账户资料或凭据。丢弃后继续？密钥不会在页面间保存。"); }
function clearSecrets() { for(const id of ["add","rotate"]) { const form=$(id).elements; if(form) { form.api_key.value=""; form.api_secret.value=""; } } }
function changeView(kind) {
  if(!discardAllowed()) { $("environment").value=viewing.environment; $("account").value=viewing.account; return; }
  ++listRead; clearSecrets(); $("add").reset?.(); notice("");
  if(kind==="environment") filterAccounts(); else showAccount();
}
$("account").addEventListener("change", ()=>changeView("account"));
$("environment").addEventListener("change", ()=>changeView("environment"));
$("refresh").addEventListener("click", async()=>{ if(!discardAllowed())return; clearSecrets(); $("add").reset?.(); $("refresh").disabled=true; try{await loadAccounts($("account").value);notice("");}catch(e){notice(e.message);}finally{$("refresh").disabled=false;} });
$("add-account").addEventListener("click",()=>{showTab("credentials",true);$("add-panel").open=true;$("rotate-panel").open=false;$("add").elements.alias.focus();});
$("edit-credentials").addEventListener("click",()=>{showTab("credentials",true);$("rotate-panel").open=true;$("add-panel").open=false;});
$("open-execution").addEventListener("click",()=>showTab("execution",true));
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
  event.preventDefault(); const form=event.currentTarget, button=form.querySelector("button"), version=generation; button.disabled=true;
  const body=Object.fromEntries(new FormData(form)); body.request_id=crypto.randomUUID();
  form.elements.api_key.value=""; form.elements.api_secret.value="";
  try { const result=await api("/api/accounts",body); notice("加密登记成功；尚未激活交易。"); if(version===generation){form.reset();await loadAccounts(result.registry_id);showTab("execution");} }
  catch(e) { notice(e.message); }
  finally { body.api_key=""; body.api_secret=""; button.disabled=false; }
});
$("rename").addEventListener("submit", async event => {
  event.preventDefault(); const a=accounts.find(x=>x.registry_id === $("account").value), version=generation; if(!a)return;
  const button=event.currentTarget.querySelector("button");button.disabled=true;
  try { await api(`/api/accounts/${a.registry_id}/alias`,{alias:$("alias").value,expected_version:a.alias_version,request_id:crypto.randomUUID()}); if(version===generation){await loadAccounts(a.registry_id);notice("别名已更新，账户历史身份不变。");} } catch(e) {notice(e.message);} finally {button.disabled=false;}
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
  const pending=result.pending_request;
  const pendingText=pending?.phase === "REJECTED" ? " · 最新切换被拒绝，当前账户保持运行："+pending.blockers.map(x=>x === "NO_AVAILABLE_BALANCE" ? "目标没有可用测试资金" : reasons[x]||x).join("；") : pending ? " · 有新切换申请等待处理" : "";
  return `${phases[result.phase] || "未知状态"}${target ? " · 账户 "+target.alias : ""} · 版本 ${result.epoch}${pendingText}${result.blockers?.length ? " · "+result.blockers.map(x=>reasons[x]||x).join("；") : ""}`;
}
async function showRoute(environment, version) {
  if(routeBusy)return;
  const read=++routeRead;
  try {
    const result=await api(`/api/accounts/execution/${environment}`);
    if(version!==generation || read!==routeRead || routeBusy)return;
    routeEpoch=result.request_epoch ?? result.epoch; $("route-state").textContent=routeText(result);
    $("route-request").disabled=environment!=="SANDBOX" || !accounts.some(a=>a.registry_id===$("account").value);
  }catch(e){if(version===generation && read===routeRead){routeEpoch=null;$("route-request").disabled=true;$("route-state").textContent="执行状态不可用；禁止申请切换。";}}
}
$("route-refresh").addEventListener("click",()=>showRoute($("environment").value || "SANDBOX",generation));
$("route-request").addEventListener("click",async()=>{
  const account=accounts.find(a=>a.registry_id===$("account").value), version=generation;
  if(!account || account.environment!=="SANDBOX" || routeEpoch===null || routeBusy)return;
  if(!window.confirm(`确认将 Testnet 执行账户切换为“${account.alias}”？不会强制平仓，也不会解除新开仓限制。`))return;
  routeBusy=true; ++routeRead;
  $("route-request").disabled=true;
  try {
    const result=await api(`/api/accounts/execution/${account.environment}`,{target_registry:account.registry_id,binding_version:account.binding_version,expected_epoch:routeEpoch,request_id:crypto.randomUUID()});
    if(version===generation){routeEpoch=result.epoch;$("route-state").textContent=routeText(result);}
  }catch(e){if(version===generation)$("route-state").textContent="申请未完成，请刷新核对状态；未授权自动切换。";}
  finally {routeBusy=false;await showRoute($("environment").value || "SANDBOX",generation);}
});
if(workspace) $("environment").value=workspace.requested.environment;
loadAccounts(workspace?.requested.account).then(()=>{if(workspace?.requested.invalid)notice("账户链接格式无效，请在顶部重新选择。");}).catch(e=>notice(e.message));
if(typeof window !== 'undefined') window.addEventListener('pageshow',event=>{
  if(event.persisted) {clearSecrets();loadAccounts($("account").value).catch(e=>notice(e.message));}
});
if(typeof window !== 'undefined') window.addEventListener('beforeunload',event=>{if(hasDraft()){event.preventDefault();event.returnValue="";}});
if(typeof setInterval === "function") setInterval(()=>{
  if(typeof document.hidden === "boolean" && document.hidden)return;
  showRoute($("environment").value || "SANDBOX",generation);
},5000);
