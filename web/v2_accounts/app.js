"use strict";
const $ = id => document.getElementById(id);
let accounts = [], generation = 0;
const notice = text => { $("notice").textContent = text; };
async function api(path, body) {
  const response = await fetch(path, {method: body ? "POST" : "GET", credentials: "same-origin", cache: "no-store", headers: body ? {"Content-Type":"application/json", "X-V2-Action":"account-management"} : {}, ...(body ? {body:JSON.stringify(body)} : {})});
  if (!response.ok) throw new Error("操作未完成，请刷新核对状态；请勿在反馈中附带密钥。");
  return response.json();
}
async function loadAccounts(selected) {
  const result = await api("/api/accounts"); accounts = result.accounts;
  for (const id of ["account","source","target"]) {
    $(id).replaceChildren(...accounts.map(a => new Option(`${a.alias} · ${a.environment}`, a.registry_id)));
  }
  if (selected && accounts.some(a=>a.registry_id === selected)) $("account").value = selected;
  await showAccount();
}
async function showAccount() {
  const version = ++generation, id = $("account").value;
  $("summary").replaceChildren(); $("trades").replaceChildren(); $("state").textContent = "";
  const account = accounts.find(a=>a.registry_id === id);
  if (!account) { $("state").textContent = "尚无已登记账户"; return; }
  $("alias").value = account.alias;
  $("state").textContent = `${account.status} · ${account.encrypted_credential_present ? "凭据已加密保存" : "尚无加密凭据"} · 未授权交易`;
  try {
    const result = await api(`/api/accounts/${id}/overview`);
    if (version !== generation || $("account").value !== result.registry_id) return;
    for (const [key, label] of [["trade_intents","交易意图"],["settled_trades","已结算交易"],["settled_net_pnl_usdt","已结算净收益 USDT"]]) {
      const p = document.createElement("p"), strong = document.createElement("strong"); p.textContent = label; strong.textContent = result.summary[key]; p.append(strong); $("summary").append(p);
    }
    for (const trade of result.trades) { const p=document.createElement("p"); p.textContent=`${trade.symbol} · ${trade.status} · ${trade.created_at} · 净收益 ${trade.net_pnl_usdt ?? "未结算"}`; $("trades").append(p); }
  } catch (e) { if(version === generation) notice(e.message); }
}
$("account").addEventListener("change", showAccount);
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
$("switch").addEventListener("submit",async event=>{
  event.preventDefault();
  try { const result=await api("/api/account-switches",{source_registry:$("source").value,target_registry:$("target").value,request_id:crypto.randomUUID()}); $("switch-state").textContent=`${result.status} · 仅创建准备单，未切换执行账户`; }catch(e){notice(e.message);}
});
loadAccounts().catch(e=>notice(e.message));
