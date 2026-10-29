/* Shared navigation and URL-only viewing context. Never stores credentials or trades. */
(() => {
  "use strict";
  const pages = {dashboard: ["交易看板", "/", "▦"], accounts: ["账户管理", "/accounts", "◎"], strategy: ["策略工作台", "/accounts/strategy", "◈"]};
  const page = document.body.dataset.page;
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const query = new URLSearchParams(window.location.search);
  let context = {environment: ["SANDBOX", "LIVE"].includes(query.get("environment")) ? query.get("environment") : "SANDBOX", account: uuid.test(query.get("account") || "") ? query.get("account") : "", days: ["1", "7", "14", "30", "90"].includes(query.get("days")) ? query.get("days") : "14"};
  const requested = Object.freeze({...context, invalid: query.has("account") && !context.account});
  function node(tag, value, cls) { const element = document.createElement(tag); if (value) element.textContent = value; if (cls) element.className = cls; return element; }
  function href(path) { const params = new URLSearchParams(); params.set("environment", context.environment); if (context.account) params.set("account", context.account); params.set("days", context.days); return path + "?" + params; }
  function nav(cls) {
    const list = node("nav", "", cls); list.setAttribute("aria-label", cls === "main-nav" ? "主导航" : "移动端主导航");
    Object.entries(pages).forEach(([id, [name, path, icon]]) => { const link = node("a", "", id === page ? "current" : ""); link.dataset.workspacePath = path; link.href = href(path); const symbol = node("span", icon); symbol.setAttribute("aria-hidden", "true"); link.append(symbol, node("span", name)); if (id === page) link.setAttribute("aria-current", "page"); list.append(link); });
    return list;
  }
  const sidebar = node("aside", "", "sidebar"); sidebar.setAttribute("aria-label", "工作空间导航");
  const brand = node("a", "", "brand"); brand.href = href("/"); brand.dataset.workspacePath = "/";
  const word = node("span", "ATLAS"); word.append(node("small", "交易工作空间")); brand.append(node("span", "A", "brand-mark"), word);
  const note = node("div", "", "sidebar-note"); note.append(node("strong", "查看与执行，明确分离"), node("p", "账户切换需单独确认\n所有时间均为 UTC+8"));
  sidebar.append(brand, node("p", "工作空间", "nav-caption"), nav("main-nav"), note);
  document.getElementById("atlas-nav").replaceChildren(sidebar);
  const header = node("header", "", "app-header"), crumb = node("div", "", "breadcrumb"); crumb.append(node("span", "工作空间"), node("span", "/"), node("strong", pages[page]?.[0] || "ATLAS"));
  const tools = node("div", "", "workspace-status"), mode = node("span", "正在读取账户…"); mode.id = "atlas-context";
  const clock = node("span", "UTC+8", "workspace-clock"); clock.id = "clock"; tools.append(mode, clock); header.append(crumb, tools);
  document.getElementById("atlas-header").replaceChildren(header, nav("mobile-nav"));
  function updateLinks() { document.querySelectorAll("[data-workspace-path]").forEach(link => { link.href = href(link.dataset.workspacePath); }); }
  function setContext(next, {writeURL = true} = {}) {
    if (["SANDBOX", "LIVE"].includes(next.environment)) context.environment = next.environment;
    if (next.account !== undefined) context.account = uuid.test(next.account || "") ? next.account : "";
    if (["1", "7", "14", "30", "90"].includes(String(next.days))) context.days = String(next.days);
    updateLinks();
    if (writeURL) window.history.replaceState(null, "", href(window.location.pathname) + window.location.hash);
  }
  function describe(alias, environment, mode = "查看") { const el = document.getElementById("atlas-context"); el.textContent = `${mode} · ${alias || "未选择账户"} · ${environment === "LIVE" ? "生产" : "Testnet"}`; el.title = el.textContent; }
  function tabs(names, fallback) {
    function show(name, focus = false) { if (!names.includes(name)) name = fallback; names.forEach(tab => { const active = tab === name, button = document.getElementById("tab-" + tab); document.getElementById("panel-" + tab).hidden = !active; button.setAttribute("aria-selected", String(active)); button.tabIndex = active ? 0 : -1; }); window.history.replaceState(null, "", window.location.pathname + window.location.search + "#" + name); if (focus) document.getElementById("tab-" + name).focus(); }
    names.forEach((name, index) => { const button = document.getElementById("tab-" + name); button.addEventListener("click", () => show(name)); button.addEventListener("keydown", e => { if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return; e.preventDefault(); show(names[e.key === "Home" ? 0 : e.key === "End" ? names.length - 1 : (index + (e.key === "ArrowRight" ? 1 : -1) + names.length) % names.length], true); }); });
    window.addEventListener("hashchange", () => show(window.location.hash.slice(1))); show(window.location.hash.slice(1)); return show;
  }
  window.Atlas = Object.freeze({requested, setContext, describe, href, tabs});
  updateLinks();
})();
