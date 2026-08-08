const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

let currentLog = "vpn";
let statusTimer = null;
let logTimer = null;
let latestStatus = null;

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { credentials: "same-origin", ...options, headers });
  let payload = {};
  try { payload = await response.json(); } catch (_) {}
  if (response.status === 401) {
    showLogin();
    throw new Error(payload.detail || "登录已过期");
  }
  if (!response.ok) throw new Error(payload.detail || `请求失败 (${response.status})`);
  return payload;
}

function toast(message, type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.innerHTML = `<strong>${type === "error" ? "错误" : "完成"}</strong>${escapeHtml(message)}`;
  $("#toast-stack").append(node);
  setTimeout(() => node.remove(), 4200);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[ch]));
}

function showLogin() {
  $("#login-screen").classList.remove("hidden");
  $("#app-shell").classList.add("hidden");
  clearInterval(statusTimer); clearInterval(logTimer);
}

function showApp() {
  $("#login-screen").classList.add("hidden");
  $("#app-shell").classList.remove("hidden");
  loadAll();
  clearInterval(statusTimer);
  statusTimer = setInterval(refreshStatus, 3000);
}

function setDot(selector, state, pending = false) {
  const node = $(selector);
  node.classList.toggle("green", state);
  node.classList.toggle("amber", pending && !state);
}

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    latestStatus = status;
    const xray = status.services.xray.running;
    const vpnProcess = status.services.vpn.running;
    const reconnectPending = Boolean(status.services.vpn.reconnect_pending);
    const connected = status.vpn_connected;
    const vpnRouteCount = (status.vpn_routes || []).filter(route => route.includes(" include ")).length;
    setDot("#top-vpn-dot", connected, vpnProcess);
    setDot("#vpn-page-dot", connected, vpnProcess);
    $("#top-vpn-text").textContent = connected ? "VPN 已连接" : vpnProcess ? "VPN 连接中" : reconnectPending ? "VPN 等待重连" : "VPN 未连接";
    $("#vpn-page-state").textContent = connected ? "已连接" : vpnProcess ? "连接中" : reconnectPending ? "等待重连" : "未连接";
    $("#route-vpn-ip").textContent = connected ? (status.vpn_ip || "隧道在线") : "等待连接";
    $("#vpn-state").textContent = connected ? "Connected" : vpnProcess ? "Connecting" : reconnectPending ? "Reconnecting" : "Disconnected";
    $("#vpn-detail").textContent = connected ? `隧道地址 ${status.vpn_ip} · ${vpnRouteCount} 条生效路由` : reconnectPending ? "短暂中断，等待自动重连" : "企业 VPN 隧道";
    $("#vpn-badge").textContent = connected ? "已连接" : vpnProcess ? "连接中" : reconnectPending ? "等待重连" : "未连接";
    $("#vpn-badge").classList.toggle("success", connected);
    $("#xray-state").textContent = xray ? "Online" : "Offline";
    $("#xray-badge").textContent = xray ? "运行中" : "已停止";
    $("#xray-badge").classList.toggle("success", xray);
    $("#overview-xray-action").textContent = xray ? "停止 Xray" : "启动 Xray";
    $("#vpn-connect").disabled = vpnProcess;
    $("#vpn-disconnect").disabled = !vpnProcess && !reconnectPending;
    const candidate = status.certificate_candidate;
    $("#certificate-trust-card").classList.toggle("hidden", !candidate);
    if (candidate) {
      $("#certificate-host").textContent = candidate.host;
      $("#certificate-pin").textContent = candidate.pin;
    }
  } catch (error) {
    if (!$("#app-shell").classList.contains("hidden")) console.debug(error);
  }
}

async function loadVpnConfig() {
  const result = await api("/api/vpn/config");
  const form = $("#vpn-form");
  const config = result.config;
  for (const key of ["server","username","authgroup","servercert","useragent","certificate","sslkey","cafile","reconnect_timeout","auto_reconnect_interval","keepalive_url","keepalive_interval"]) {
    if (form.elements[key]) form.elements[key].value = config[key] ?? "";
  }
  for (const key of ["no_dtls","disable_ipv6","auto_reconnect","keepalive_enabled","autostart"]) form.elements[key].checked = Boolean(config[key]);
  form.elements.route_mode.value = config.route_mode || "all";
  form.elements.manual_routes.value = (config.manual_routes || []).join("\n");
  form.elements.manual_exclude_routes.value = (config.manual_exclude_routes || []).join("\n");
  form.elements.extra_args.value = (config.extra_args || []).join("\n");
  updateRouteModeFields();
  $("#saved-password-badge").textContent = result.has_password ? "已有保存密码" : "无已存密码";
  $("#saved-password-badge").classList.toggle("success", result.has_password);
}

async function loadXrayConfig() {
  const result = await api("/api/xray/config");
  $("#xray-editor").value = JSON.stringify(result.config, null, 2);
  updateLineNumbers();
}

async function loadAll() {
  await Promise.allSettled([refreshStatus(), loadVpnConfig(), loadXrayConfig()]);
}

function switchPage(page) {
  $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.page === page));
  $$(".page").forEach(item => item.classList.toggle("active", item.id === `page-${page}`));
  $("#page-title").textContent = ({overview:"运行总览",vpn:"VPN 配置",xray:"Xray 配置",logs:"实时日志"})[page];
  $(".sidebar").classList.remove("open");
  if (page === "logs") startLogs(); else stopLogs();
}

function formToVpnConfig() {
  const form = $("#vpn-form");
  return {
    server: form.elements.server.value.trim(),
    username: form.elements.username.value.trim(),
    authgroup: form.elements.authgroup.value.trim(),
    servercert: form.elements.servercert.value.trim(),
    useragent: form.elements.useragent.value.trim(),
    certificate: form.elements.certificate.value.trim(),
    sslkey: form.elements.sslkey.value.trim(),
    cafile: form.elements.cafile.value.trim(),
    no_dtls: form.elements.no_dtls.checked,
    disable_ipv6: form.elements.disable_ipv6.checked,
    route_mode: form.elements.route_mode.value,
    manual_routes: form.elements.manual_routes.value.split("\n").map(v => v.trim()).filter(Boolean),
    manual_exclude_routes: form.elements.manual_exclude_routes.value.split("\n").map(v => v.trim()).filter(Boolean),
    reconnect_timeout: Number(form.elements.reconnect_timeout.value || 300),
    auto_reconnect: form.elements.auto_reconnect.checked,
    auto_reconnect_interval: Number(form.elements.auto_reconnect_interval.value || 10),
    keepalive_enabled: form.elements.keepalive_enabled.checked,
    keepalive_url: form.elements.keepalive_url.value.trim(),
    keepalive_interval: Number(form.elements.keepalive_interval.value || 300),
    extra_args: form.elements.extra_args.value.split("\n").map(v => v.trim()).filter(Boolean),
    autostart: form.elements.autostart.checked,
  };
}

function updateRouteModeFields() {
  const manual = $("#vpn-form").elements.route_mode.value === "manual";
  $$(`[data-manual-route]`).forEach(field => field.classList.toggle("hidden", !manual));
}

async function saveVpn(includePassword = false) {
  const config = formToVpnConfig();
  const password = $("#connect-password").value;
  if (includePassword && password) config.password = password;
  await api("/api/vpn/config", { method:"PUT", body:{ config, save_password: includePassword && Boolean(password) } });
  await loadVpnConfig();
}

function parseEditor() {
  try {
    const config = JSON.parse($("#xray-editor").value);
    $("#editor-status").textContent = "JSON 有效";
    return config;
  } catch (error) {
    $("#editor-status").textContent = "JSON 错误";
    throw new Error(`JSON 格式错误：${error.message}`);
  }
}

function updateLineNumbers() {
  const editor = $("#xray-editor");
  const count = editor.value.split("\n").length;
  $("#line-numbers").textContent = Array.from({length:count}, (_,i) => i + 1).join("\n");
  try { JSON.parse(editor.value); $("#editor-status").textContent = "JSON 有效"; }
  catch (_) { $("#editor-status").textContent = "JSON 错误"; }
}

async function refreshLogs() {
  try {
    const result = await api(`/api/logs/${currentLog}`);
    const output = $("#log-output");
    const nearBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 80;
    output.textContent = result.lines.length ? result.lines.join("\n") : "尚无日志输出。";
    if (nearBottom) output.scrollTop = output.scrollHeight;
  } catch (_) {}
}

function startLogs() {
  refreshLogs(); clearInterval(logTimer); logTimer = setInterval(refreshLogs, 2000);
}
function stopLogs() { clearInterval(logTimer); }

$("#login-form").addEventListener("submit", async event => {
  event.preventDefault(); $("#login-error").textContent = "";
  try {
    await api("/api/login", { method:"POST", body:{ username:$("#login-username").value, password:$("#login-password").value } });
    $("#login-password").value = ""; showApp();
  } catch (error) { $("#login-error").textContent = error.message; }
});

$("#logout-button").addEventListener("click", async () => { try { await api("/api/logout", {method:"POST"}); } finally { showLogin(); } });
$$(".nav-item").forEach(item => item.addEventListener("click", () => switchPage(item.dataset.page)));
$$(`[data-go]`).forEach(item => item.addEventListener("click", () => switchPage(item.dataset.go)));
$("#menu-button").addEventListener("click", () => $(".sidebar").classList.toggle("open"));
$("#vpn-form").elements.route_mode.addEventListener("change", updateRouteModeFields);

$("#vpn-form").addEventListener("submit", async event => {
  event.preventDefault();
  try { await saveVpn(false); toast("VPN 配置已保存；路由模式将在下次连接时生效"); }
  catch (error) { toast(error.message, "error"); }
});

$("#vpn-connect").addEventListener("click", async () => {
  try {
    await saveVpn($("#save-password").checked);
    await api("/api/vpn/connect", { method:"POST", body:{ password:$("#connect-password").value, otp:$("#connect-otp").value } });
    $("#connect-otp").value = "";
    if (!$("#save-password").checked) $("#connect-password").value = "";
    toast("VPN 连接进程已启动"); refreshStatus();
  } catch (error) { toast(error.message, "error"); }
});

$("#vpn-disconnect").addEventListener("click", async () => {
  try { await api("/api/vpn/disconnect", {method:"POST"}); toast("VPN 已断开，Xray 出站已回落普通网络"); refreshStatus(); }
  catch (error) { toast(error.message, "error"); }
});

$("#trust-certificate").addEventListener("click", async () => {
  const host = latestStatus?.certificate_candidate?.host || "当前 VPN 网关";
  const confirmed = window.confirm(`仅在你确认指纹来源可信时继续。是否固定 ${host} 的当前证书公钥指纹？`);
  if (!confirmed) return;
  try {
    const result = await api("/api/vpn/trust-certificate", {method:"POST"});
    await loadVpnConfig();
    await refreshStatus();
    toast(`已信任 ${result.candidate.host}，请重新输入密码连接`);
  } catch (error) { toast(error.message, "error"); }
});

$("#overview-xray-action").addEventListener("click", async () => {
  const action = latestStatus?.services?.xray?.running ? "stop" : "start";
  try { await api(`/api/xray/${action}`, {method:"POST"}); toast(`Xray 已${action === "start" ? "启动" : "停止"}`); refreshStatus(); }
  catch (error) { toast(error.message, "error"); }
});

$("#xray-validate").addEventListener("click", async () => {
  try { const result = await api("/api/xray/validate", {method:"POST",body:parseEditor()}); toast(result.message); }
  catch (error) { toast(error.message, "error"); }
});

$("#xray-save").addEventListener("click", async () => {
  try {
    const config = parseEditor();
    const result = await api("/api/xray/config", {method:"PUT",body:config});
    await api("/api/xray/restart", {method:"POST"});
    toast(result.message || "Xray 配置已保存并重启"); refreshStatus();
  } catch (error) { toast(error.message, "error"); }
});

$("#xray-editor").addEventListener("input", updateLineNumbers);
$("#xray-editor").addEventListener("scroll", event => { $("#line-numbers").scrollTop = event.target.scrollTop; });
$("#xray-editor").addEventListener("keydown", event => {
  if (event.key === "Tab") {
    event.preventDefault(); const editor = event.target; const start = editor.selectionStart;
    editor.value = editor.value.slice(0,start) + "  " + editor.value.slice(editor.selectionEnd);
    editor.selectionStart = editor.selectionEnd = start + 2; updateLineNumbers();
  }
});

$$(`[data-log]`).forEach(button => button.addEventListener("click", () => {
  currentLog = button.dataset.log;
  $$(`[data-log]`).forEach(item => item.classList.toggle("active", item === button));
  $("#terminal-title").textContent = currentLog === "vpn" ? "openconnect — live" : "xray-core — live";
  refreshLogs();
}));
$("#refresh-logs").addEventListener("click", refreshLogs);

api("/api/me").then(showApp).catch(showLogin);
