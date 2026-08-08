const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

let currentLog = "vpn";
let statusTimer = null;
let logTimer = null;
let targetHistoryTimer = null;
let latestStatus = null;
let latestTargetHistory = null;
let trafficHistory = [];
let historySearchTimer = null;
const historyView = { query:"", route:"all", sort:"frequency", page:1, pageSize:20 };
const historyCollator = new Intl.Collator(undefined, { numeric:true, sensitivity:"base" });
const historyRouteSearchTerms = {
  vpn:"vpn 隧道",
  direct:"直连 direct",
  unknown:"未识别 unknown",
};

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
  clearInterval(statusTimer); clearInterval(logTimer); clearInterval(targetHistoryTimer);
}

function showApp() {
  $("#login-screen").classList.add("hidden");
  $("#app-shell").classList.remove("hidden");
  loadAll();
  clearInterval(statusTimer);
  statusTimer = setInterval(refreshStatus, 3000);
  clearInterval(targetHistoryTimer);
  targetHistoryTimer = setInterval(loadTargetHistory, 30000);
}

function setDot(selector, state, pending = false) {
  const node = $(selector);
  node.classList.toggle("green", state);
  node.classList.toggle("amber", pending && !state);
}

function updateRouteMap(status, connected, vpnProcess, reconnectPending) {
  const inbounds = Array.isArray(status.xray_inbounds) ? status.xray_inbounds : [];
  const labels = [...new Set(inbounds.map(inbound => inbound.label).filter(Boolean))];
  const ports = inbounds.map(inbound => inbound.port).filter(port => port !== null && port !== undefined && port !== "");
  const protocolText = labels.length ? labels.join(" / ") : "未配置入站";
  const portText = ports.length ? ports.join(" · ") : "—";
  $("#route-inbound-protocols").textContent = protocolText;
  $("#route-inbound-ports").textContent = portText;
  const inboundNode = $("#route-inbound-node");
  inboundNode.classList.toggle("dense", labels.length > 2 || protocolText.length > 18);
  inboundNode.title = inbounds.length
    ? inbounds.map(inbound => `${inbound.label} ${inbound.listen ? `${inbound.listen}:` : ""}${inbound.port ?? ""}`).join("\n")
    : "当前 Xray 配置没有可显示的 inbound";

  $("#route-vpn-node").classList.toggle("connected", connected);
  $("#route-line").classList.toggle("active", connected);
  $("#route-vpn-ip").textContent = connected
    ? (status.vpn_ip || "隧道在线")
    : vpnProcess
      ? "正在建立隧道"
      : reconnectPending
        ? "等待重连"
        : "未连接";
}

function formatBytes(value) {
  const bytes = Math.max(0, Number(value) || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = bytes;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  const digits = index === 0 || amount >= 100 ? 0 : amount >= 10 ? 1 : 2;
  return `${amount.toFixed(digits)} ${units[index]}`;
}

function formatRate(value) {
  return `${formatBytes(value)}/s`;
}

function formatDurationSince(timestamp, fallback = "—") {
  if (!timestamp) return fallback;
  let seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(timestamp)));
  const days = Math.floor(seconds / 86400);
  seconds %= 86400;
  const hours = Math.floor(seconds / 3600);
  seconds %= 3600;
  const minutes = Math.floor(seconds / 60);
  seconds %= 60;
  if (days) return `${days}天 ${hours}小时`;
  if (hours) return `${hours}小时 ${minutes}分`;
  if (minutes) return `${minutes}分 ${seconds}秒`;
  return `${seconds}秒`;
}

function formatTimestamp(timestamp) {
  if (!timestamp) return "";
  return new Date(Number(timestamp) * 1000).toLocaleString("zh-CN", {
    month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false,
  });
}

function formatRelativeTime(timestamp, emptyText = "尚无记录") {
  if (!timestamp) return emptyText;
  const delta = Math.round(Date.now() / 1000 - Number(timestamp));
  const future = delta < 0;
  const seconds = Math.abs(delta);
  let value;
  if (seconds < 5) value = "刚刚";
  else if (seconds < 60) value = `${seconds} 秒`;
  else if (seconds < 3600) value = `${Math.floor(seconds / 60)} 分钟`;
  else if (seconds < 86400) value = `${Math.floor(seconds / 3600)} 小时`;
  else value = `${Math.floor(seconds / 86400)} 天`;
  if (value === "刚刚") return value;
  return future ? `${value}后` : `${value}前`;
}

function setTimestampText(selector, text, timestamp) {
  const node = $(selector);
  node.textContent = text;
  node.title = timestamp ? formatTimestamp(timestamp) : "";
}

function targetAddressSubtitle(target) {
  const resolved = Array.isArray(target.addresses)
    ? target.addresses
    : [target.address].filter(Boolean);
  const route = target.route === "vpn"
    ? "VPN"
    : target.route === "direct" ? "直连" : "未识别";
  const destination = target.domain && resolved.length
    ? `IP ${resolved.join(" · ")}`
    : target.scope === "public"
      ? "公网地址"
      : target.scope === "private" ? "内网或本地地址" : "目标地址";
  const outbound = target.outbound_tag ? ` · Xray ${target.outbound_tag}` : "";
  return `${route} · ${destination}${outbound}`;
}

function renderTargetConnections(targets) {
  const list = $("#target-list");
  const addresses = Array.isArray(targets?.addresses) ? targets.addresses : [];
  $("#target-count-badge").textContent = `${Number(targets?.unique_endpoints) || 0} 个端点`;
  if (!addresses.length) {
    const empty = document.createElement("div");
    empty.className = "target-empty";
    empty.textContent = "暂无活动的 TCP 目标连接";
    list.replaceChildren(empty);
    return;
  }
  const rows = addresses.map(target => {
    const row = document.createElement("div");
    row.className = `target-row route-${target.route || "unknown"}`;
    const address = document.createElement("div");
    address.className = "target-address";
    const endpoint = document.createElement("code");
    endpoint.textContent = target.endpoint || `${target.address}:${target.port}`;
    endpoint.title = endpoint.textContent;
    const scope = document.createElement("small");
    scope.textContent = targetAddressSubtitle(target);
    address.append(endpoint, scope);
    const count = document.createElement("span");
    count.className = "target-count";
    const dot = document.createElement("i");
    const label = document.createElement("span");
    label.textContent = `${Number(target.connections) || 0} 条`;
    count.append(dot, label);
    row.append(address, count);
    return row;
  });
  list.replaceChildren(...rows);
}

function renderTrafficChart() {
  const canvas = $("#traffic-chart");
  if (!canvas) return;
  const bounds = canvas.getBoundingClientRect();
  if (!bounds.width) return;
  const width = Math.floor(bounds.width);
  const height = Math.floor(bounds.height || 180);
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);

  const padding = { left:12, right:12, top:20, bottom:16 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  context.strokeStyle = "#202a33";
  context.lineWidth = 1;
  for (let index = 0; index <= 3; index += 1) {
    const y = padding.top + chartHeight * index / 3;
    context.beginPath();
    context.moveTo(padding.left, y + .5);
    context.lineTo(width - padding.right, y + .5);
    context.stroke();
  }

  if (trafficHistory.length < 2) {
    context.fillStyle = "#62707b";
    context.font = "10px system-ui, sans-serif";
    context.textAlign = "center";
    context.fillText("等待实时流量样本", width / 2, height / 2);
    return;
  }

  const maximum = Math.max(1, ...trafficHistory.flatMap(sample => [sample.rx, sample.tx]));
  context.fillStyle = "#5d6973";
  context.font = "8px system-ui, sans-serif";
  context.textAlign = "right";
  context.fillText(formatRate(maximum), width - padding.right, 11);
  const drawLine = (key, color) => {
    context.beginPath();
    trafficHistory.forEach((sample, index) => {
      const x = padding.left + chartWidth * index / Math.max(trafficHistory.length - 1, 1);
      const y = padding.top + chartHeight - (sample[key] / maximum) * chartHeight;
      if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
    });
    context.strokeStyle = color;
    context.lineWidth = 2;
    context.lineJoin = "round";
    context.lineCap = "round";
    context.stroke();
  };
  drawLine("rx", "#9bef4d");
  drawLine("tx", "#6ba3ff");
}

function renderTargetHistory(history) {
  latestTargetHistory = history;
  const targets = Array.isArray(history.targets) ? history.targets : [];
  const query = historyView.query.trim().toLocaleLowerCase();
  const filteredTargets = targets.filter(target => {
    if (historyView.route !== "all" && target.route !== historyView.route) return false;
    if (!query) return true;
    return [
      target.endpoint,
      target.domain,
      target.address,
      ...(Array.isArray(target.addresses) ? target.addresses : []),
      target.port,
      target.outbound_tag,
      historyRouteSearchTerms[target.route] || historyRouteSearchTerms.unknown,
    ].filter(Boolean).join(" ").toLocaleLowerCase().includes(query);
  });
  filteredTargets.sort((left, right) => {
    if (historyView.sort === "recent") {
      return (Number(right.last_seen) || 0) - (Number(left.last_seen) || 0)
        || (Number(right.connections) || 0) - (Number(left.connections) || 0);
    }
    if (historyView.sort === "active-days") {
      return (Number(right.active_days) || 0) - (Number(left.active_days) || 0)
        || (Number(right.connections) || 0) - (Number(left.connections) || 0);
    }
    if (historyView.sort === "endpoint") {
      return historyCollator.compare(left.endpoint || "", right.endpoint || "");
    }
    return (Number(right.connections) || 0) - (Number(left.connections) || 0)
      || (Number(right.last_seen) || 0) - (Number(left.last_seen) || 0);
  });
  const pageCount = Math.max(1, Math.ceil(filteredTargets.length / historyView.pageSize));
  historyView.page = Math.min(Math.max(1, historyView.page), pageCount);
  const pageStart = (historyView.page - 1) * historyView.pageSize;
  const visibleTargets = filteredTargets.slice(pageStart, pageStart + historyView.pageSize);
  const retentionDays = Number(history.retention_days) || 30;
  $("#history-range").textContent = `最近 ${retentionDays} 天`;
  $("#history-total").textContent = `${Number(history.total_connections) || 0} 次`;
  $("#history-addresses").textContent = `${Number(history.unique_addresses) || 0} 个`;
  $("#history-endpoints").textContent = `${Number(history.unique_endpoints) || 0} 个`;
  $("#history-active-days").textContent = `${Number(history.active_days) || 0} 天`;
  $("#history-description").textContent = history.vpn?.server
    ? `绑定到 ${history.vpn.server} · VPN ${Number(history.vpn_connections) || 0} 次 · 直连 ${Number(history.direct_connections) || 0} 次 · 未识别 ${Number(history.unknown_connections) || 0} 次。`
    : "填写 VPN 服务器后，将按当前 VPN 配置独立累计目标连接。";

  const filteredConnections = filteredTargets.reduce(
    (total, target) => total + (Number(target.connections) || 0),
    0,
  );
  const loadedRows = targets.length;
  const totalRows = Number(history.total_target_rows) || loadedRows;
  const visibleStart = filteredTargets.length ? pageStart + 1 : 0;
  const visibleEnd = Math.min(pageStart + historyView.pageSize, filteredTargets.length);
  $("#history-result-count").textContent = history.targets_truncated
    ? `显示 ${visibleStart}–${visibleEnd} / 已载入 ${loadedRows} 项（共 ${totalRows} 项） · ${filteredConnections} 次访问`
    : `显示 ${visibleStart}–${visibleEnd} / ${filteredTargets.length} 项 · ${filteredConnections} 次访问`;
  $("#history-page-status").textContent = `${historyView.page} / ${pageCount}`;
  $("#history-prev").disabled = historyView.page <= 1;
  $("#history-next").disabled = historyView.page >= pageCount;
  $("#history-pagination").classList.toggle("hidden", pageCount <= 1);
  $("#history-reset").disabled = !(
    historyView.query || historyView.route !== "all" || historyView.sort !== "frequency"
  );
  $$('[data-history-route]').forEach(button => {
    const active = button.dataset.historyRoute === historyView.route;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });

  const list = $("#history-list");
  if (!visibleTargets.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = targets.length
      ? "没有符合当前筛选条件的目标记录"
      : "当前 VPN 暂无历史目标记录";
    list.replaceChildren(empty);
    return;
  }
  const maximum = Math.max(1, ...filteredTargets.map(target => Number(target.connections) || 0));
  const rows = visibleTargets.map(target => {
    const row = document.createElement("div");
    row.className = `history-row route-${target.route || "unknown"}`;
    const endpointCell = document.createElement("div");
    endpointCell.className = "history-endpoint";
    const endpoint = document.createElement("code");
    endpoint.textContent = target.endpoint || `${target.address}:${target.port}`;
    endpoint.title = endpoint.textContent;
    const scope = document.createElement("small");
    scope.textContent = targetAddressSubtitle(target);
    endpointCell.append(endpoint, scope);
    const days = document.createElement("span");
    days.className = "history-cell history-days";
    days.textContent = `${Number(target.active_days) || 0} 天`;
    const last = document.createElement("span");
    last.className = "history-cell history-last";
    last.textContent = formatRelativeTime(target.last_seen);
    last.title = formatTimestamp(target.last_seen);
    const count = document.createElement("strong");
    count.className = "history-count";
    count.textContent = `${Number(target.connections) || 0} 次`;
    const frequency = document.createElement("progress");
    frequency.className = "history-frequency";
    frequency.max = maximum;
    frequency.value = Number(target.connections) || 0;
    frequency.setAttribute("aria-label", `${endpoint.textContent} 访问频次`);
    row.append(endpointCell, days, last, count, frequency);
    return row;
  });
  list.replaceChildren(...rows);
}

async function loadTargetHistory() {
  const panel = $(".history-panel");
  panel.setAttribute("aria-busy", "true");
  try {
    renderTargetHistory(await api("/api/statistics/targets"));
  } catch (_) {
    if (!latestTargetHistory) $("#history-result-count").textContent = "历史数据加载失败";
  } finally {
    panel.removeAttribute("aria-busy");
  }
}

function refreshTargetHistoryView() {
  if (latestTargetHistory) renderTargetHistory(latestTargetHistory);
}

function updateHistoryView(values) {
  Object.assign(historyView, values, { page:1 });
  refreshTargetHistoryView();
}

function updateOverviewStatistics(status) {
  const statistics = status.statistics || {};
  const session = statistics.vpn_session || {};
  const traffic = statistics.traffic || {};
  const connections = statistics.connections || {};
  const clients = connections.clients || connections;
  const targets = connections.targets || {};
  const vpn = status.services.vpn || {};
  const xray = status.services.xray || {};
  const keepalive = status.keepalive || {};

  $("#stat-vpn-uptime").textContent = status.vpn_connected ? formatDurationSince(session.connected_at, "已连接") : "未连接";
  setTimestampText("#stat-vpn-since", session.connected_at ? `始于 ${formatTimestamp(session.connected_at)}` : "等待建立隧道", session.connected_at);
  $("#stat-traffic-total").textContent = traffic.available ? `↓ ${formatBytes(traffic.rx_bytes)} · ↑ ${formatBytes(traffic.tx_bytes)}` : "暂无接口数据";
  $("#stat-traffic-rate").textContent = `↓ ${formatRate(traffic.rx_rate)} · ↑ ${formatRate(traffic.tx_rate)}`;
  $("#stat-targets").textContent = Number(targets.active) || 0;
  $("#stat-connections").textContent = `${Number(targets.vpn_active) || 0} VPN · ${Number(targets.direct_active) || 0} 直连 · ${Number(targets.unknown_active) || 0} 未识别 · ${Number(clients.unique_addresses) || 0} 客户端`;
  $("#stat-retry-count").textContent = `${Number(vpn.reconnect_attempts_total) || 0} 次`;
  setTimestampText("#stat-retry-detail", vpn.next_retry_at ? `下次重试 ${formatRelativeTime(vpn.next_retry_at)}` : vpn.last_retry_at ? `上次重试 ${formatRelativeTime(vpn.last_retry_at)}` : "尚未重试", vpn.next_retry_at || vpn.last_retry_at);

  $("#traffic-rx-total").textContent = formatBytes(traffic.rx_bytes);
  $("#traffic-tx-total").textContent = formatBytes(traffic.tx_bytes);
  $("#traffic-rx-packets").textContent = `${Number(traffic.rx_packets) || 0} 个数据包`;
  $("#traffic-tx-packets").textContent = `${Number(traffic.tx_packets) || 0} 个数据包`;
  $("#traffic-errors").textContent = String((Number(traffic.rx_errors) || 0) + (Number(traffic.tx_errors) || 0));

  if (!status.vpn_connected) trafficHistory = [];
  if (traffic.available) {
    const sampledAt = Number(statistics.sampled_at) || Date.now() / 1000;
    const lastSample = trafficHistory[trafficHistory.length - 1];
    if (!lastSample || lastSample.at !== sampledAt) {
      trafficHistory.push({ at:sampledAt, rx:Number(traffic.rx_rate) || 0, tx:Number(traffic.tx_rate) || 0 });
      trafficHistory = trafficHistory.slice(-40);
    }
  }
  renderTrafficChart();
  renderTargetConnections(targets);

  $("#fact-xray-uptime").textContent = xray.running ? formatDurationSince(xray.started_at, "运行中") : "未运行";
  $("#fact-route-count").textContent = `${Number(session.route_count) || 0} 条`;
  $("#fact-dns-count").textContent = session.dns_count ? `${session.dns_count} 个` : "容器默认";
  const keepaliveText = keepalive.last_at ? `${keepalive.ok ? "成功" : "失败"} · ${formatRelativeTime(keepalive.last_at)}` : "尚无记录";
  setTimestampText("#fact-keepalive", keepaliveText, keepalive.last_at);
  setTimestampText("#fact-last-retry", formatRelativeTime(vpn.last_retry_at), vpn.last_retry_at);
  setTimestampText("#fact-next-retry", formatRelativeTime(vpn.next_retry_at, "未安排"), vpn.next_retry_at);
  $("#statistics-freshness").textContent = "刚刚更新";
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
    const activeDns = status.active_dns || [];
    const usesDefaultProxyPassword = status.security?.default_proxy_password === true;
    $("#overview-security-notice").classList.toggle("hidden", !usesDefaultProxyPassword);
    updateRouteMap(status, connected, vpnProcess, reconnectPending);
    setDot("#top-vpn-dot", connected, vpnProcess);
    setDot("#vpn-page-dot", connected, vpnProcess);
    $("#top-vpn-text").textContent = connected ? "VPN 已连接" : vpnProcess ? "VPN 连接中" : reconnectPending ? "VPN 等待重连" : "VPN 未连接";
    $("#vpn-page-state").textContent = connected ? "已连接" : vpnProcess ? "连接中" : reconnectPending ? "等待重连" : "未连接";
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
    $("#active-dns").textContent = activeDns.length ? activeDns.join(" · ") : "容器默认 DNS";
    updateOverviewStatistics(status);
    const candidate = status.certificate_candidate;
    $("#certificate-trust-card").classList.toggle("hidden", !candidate);
    if (candidate) {
      $("#certificate-host").textContent = candidate.host;
      $("#certificate-pin").textContent = candidate.pin;
    }
  } catch (_) {}
}

async function loadVpnConfig() {
  const result = await api("/api/vpn/config");
  const form = $("#vpn-form");
  const config = result.config;
  for (const key of ["server","username","authgroup","servercert","useragent","certificate","sslkey","cafile","reconnect_timeout","auto_reconnect_interval","keepalive_url","keepalive_interval","statistics_retention_days"]) {
    if (form.elements[key]) form.elements[key].value = config[key] ?? "";
  }
  for (const key of ["no_dtls","disable_ipv6","auto_reconnect","keepalive_enabled","autostart"]) form.elements[key].checked = Boolean(config[key]);
  form.elements.route_mode.value = config.route_mode || "all";
  form.elements.manual_routes.value = (config.manual_routes || []).join("\n");
  form.elements.manual_exclude_routes.value = (config.manual_exclude_routes || []).join("\n");
  form.elements.dns_mode.value = config.dns_mode || "system";
  form.elements.dns_servers.value = (config.dns_servers || []).join("\n");
  form.elements.extra_args.value = (config.extra_args || []).join("\n");
  updateRouteModeFields();
  updateKeepaliveFields();
  updateDnsModeFields();
  $("#saved-password-badge").textContent = result.has_password ? "已有保存密码" : "无已存密码";
  $("#saved-password-badge").classList.toggle("success", result.has_password);
}

async function loadXrayConfig() {
  const result = await api("/api/xray/config");
  $("#xray-editor").value = JSON.stringify(result.config, null, 2);
  updateLineNumbers();
}

async function loadAll() {
  await Promise.allSettled([refreshStatus(), loadVpnConfig(), loadXrayConfig(), loadTargetHistory()]);
}

function switchPage(page) {
  $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.page === page));
  $$(".page").forEach(item => item.classList.toggle("active", item.id === `page-${page}`));
  $("#page-title").textContent = ({overview:"运行总览",vpn:"VPN 配置",xray:"Xray 配置",logs:"实时日志"})[page];
  $(".sidebar").classList.remove("open");
  if (page === "overview") {
    requestAnimationFrame(renderTrafficChart);
    loadTargetHistory();
  }
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
    dns_mode: form.elements.dns_mode.value,
    dns_servers: form.elements.dns_servers.value.replaceAll(",", "\n").split("\n").map(v => v.trim()).filter(Boolean),
    reconnect_timeout: Number(form.elements.reconnect_timeout.value || 300),
    auto_reconnect: form.elements.auto_reconnect.checked,
    auto_reconnect_interval: Number(form.elements.auto_reconnect_interval.value || 10),
    keepalive_enabled: form.elements.keepalive_enabled.checked,
    keepalive_url: form.elements.keepalive_url.value.trim(),
    keepalive_interval: Number(form.elements.keepalive_interval.value || 300),
    statistics_retention_days: Number(form.elements.statistics_retention_days.value || 30),
    extra_args: form.elements.extra_args.value.split("\n").map(v => v.trim()).filter(Boolean),
    autostart: form.elements.autostart.checked,
  };
}

function updateRouteModeFields() {
  const manual = $("#vpn-form").elements.route_mode.value === "manual";
  $$(`[data-manual-route]`).forEach(field => field.classList.toggle("hidden", !manual));
}

function updateKeepaliveFields() {
  const enabled = $("#vpn-form").elements.keepalive_enabled.checked;
  $$(`[data-keepalive-field]`).forEach(field => {
    field.classList.toggle("disabled", !enabled);
    const control = field.querySelector("input");
    if (control) control.disabled = !enabled;
  });
}

function updateDnsModeFields() {
  const manual = $("#vpn-form").elements.dns_mode.value === "manual";
  $$(`[data-manual-dns]`).forEach(field => field.classList.toggle("hidden", !manual));
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
$("#history-query").addEventListener("input", event => {
  clearTimeout(historySearchTimer);
  historySearchTimer = setTimeout(() => {
    updateHistoryView({ query:event.target.value });
  }, 160);
});
$$('[data-history-route]').forEach(button => button.addEventListener("click", () => {
  updateHistoryView({ route:button.dataset.historyRoute });
}));
$("#history-sort").addEventListener("change", event => {
  updateHistoryView({ sort:event.target.value });
});
$("#history-reset").addEventListener("click", () => {
  clearTimeout(historySearchTimer);
  $("#history-query").value = "";
  $("#history-sort").value = "frequency";
  updateHistoryView({ query:"", route:"all", sort:"frequency" });
});
$("#history-prev").addEventListener("click", () => {
  historyView.page -= 1;
  refreshTargetHistoryView();
});
$("#history-next").addEventListener("click", () => {
  historyView.page += 1;
  refreshTargetHistoryView();
});
$("#vpn-form").elements.route_mode.addEventListener("change", updateRouteModeFields);
$("#vpn-form").elements.keepalive_enabled.addEventListener("change", updateKeepaliveFields);
$("#vpn-form").elements.dns_mode.addEventListener("change", updateDnsModeFields);

$("#vpn-form").addEventListener("submit", async event => {
  event.preventDefault();
  try { await saveVpn(false); toast("配置已保存；DNS 与路由将在下次连接时生效，保活设置已更新"); }
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
window.addEventListener("resize", renderTrafficChart);

api("/api/me").then(showApp).catch(showLogin);
