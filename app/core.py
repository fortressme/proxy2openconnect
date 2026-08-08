from __future__ import annotations

import copy
import base64
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
XRAY_CONFIG = DATA_DIR / "xray" / "config.json"
VPN_CONFIG = DATA_DIR / "vpn" / "config.json"
RUNTIME_CONFIG = Path("/run/proxy2openconnect/xray-effective.json")
VPN_CONNECTED = Path("/run/proxy2openconnect/vpn.connected")
VPN_ROUTES = Path("/run/proxy2openconnect/split-routes")
DNS_ACTIVE = Path("/run/proxy2openconnect/dns-active")
XRAY_MARK = int(os.getenv("XRAY_VPN_MARK", "255"))
ROUTE_TABLE = int(os.getenv("XRAY_VPN_ROUTE_TABLE", "200"))
VPN_OUTBOUND_TAGS = frozenset(
    tag.strip()
    for tag in os.getenv("XRAY_VPN_OUTBOUND_TAGS", "vpn-out").split(",")
    if tag.strip()
)
XRAY_BINARY = os.getenv("XRAY_BINARY", "/usr/local/bin/xray")
OPENCONNECT_BINARY = os.getenv("OPENCONNECT_BINARY", "/usr/sbin/openconnect")
VPN_SCRIPT = "/opt/proxy2openconnect/scripts/vpn-script.sh"
VPN_ROUTE_MODES = frozenset({"all", "vpn", "manual"})
VPN_DNS_MODES = frozenset({"system", "vpn", "manual"})
MAX_MANUAL_ROUTES = 4096
MAX_AUTO_RECONNECT_ATTEMPTS = 5


class ConfigError(ValueError):
    pass


CERT_HOST_PATTERN = re.compile(r'Certificate from VPN server "([^"\r\n]+)"')
CERT_PIN_PATTERN = re.compile(r'--servercert\s+(pin-sha256:[A-Za-z0-9+/]{43}=)')


def extract_certificate_candidate(lines: list[str]) -> dict[str, str] | None:
    """Extract the newest OpenConnect certificate pin together with its gateway host."""
    current_host: str | None = None
    candidate: dict[str, str] | None = None
    for line in lines:
        host_match = CERT_HOST_PATTERN.search(line)
        if host_match:
            current_host = host_match.group(1).strip().lower()
        pin_match = CERT_PIN_PATTERN.search(line)
        if pin_match and current_host:
            pin = pin_match.group(1)
            try:
                decoded = base64.b64decode(pin.removeprefix("pin-sha256:"), validate=True)
            except ValueError:
                continue
            if len(decoded) == 32:
                candidate = {"host": current_host, "pin": pin}
    return candidate


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"JSON 第 {exc.lineno} 行第 {exc.colno} 列有误: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ConfigError("配置顶层必须是 JSON 对象")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def effective_xray_config(
    config: dict[str, Any],
    mark: int = XRAY_MARK,
    outbound_tags: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Return a runtime copy where only selected outbounds are VPN-marked."""
    result = copy.deepcopy(config)
    outbounds = result.get("outbounds")
    if not isinstance(outbounds, list) or not outbounds:
        raise ConfigError("Xray 配置至少需要一个 outbound")

    selected_tags = VPN_OUTBOUND_TAGS if outbound_tags is None else outbound_tags
    for index, outbound in enumerate(outbounds):
        if not isinstance(outbound, dict):
            raise ConfigError(f"outbounds[{index}] 必须是对象")
        if outbound.get("protocol") == "blackhole" or outbound.get("tag") not in selected_tags:
            continue
        stream = outbound.setdefault("streamSettings", {})
        if not isinstance(stream, dict):
            raise ConfigError(f"outbounds[{index}].streamSettings 必须是对象")
        sockopt = stream.setdefault("sockopt", {})
        if not isinstance(sockopt, dict):
            raise ConfigError(f"outbounds[{index}].streamSettings.sockopt 必须是对象")
        sockopt["mark"] = mark
    return result


def validate_xray_shape(config: dict[str, Any]) -> None:
    if not isinstance(config.get("inbounds"), list) or not config["inbounds"]:
        raise ConfigError("Xray 配置至少需要一个 inbound")
    effective_xray_config(config)


def validate_server(server: str) -> str:
    server = server.strip()
    if not server:
        raise ConfigError("请填写 VPN 服务器")
    candidate = server if "://" in server else f"https://{server}"
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ConfigError("VPN 服务器必须是 HTTPS 地址或有效主机名")
    if parsed.username or parsed.password:
        raise ConfigError("请勿在 VPN 地址中包含用户名或密码")
    return candidate


def normalize_http_origin(value: str) -> str:
    candidate = value.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigError("可信来源必须是有效的 HTTP 或 HTTPS 来源")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("可信来源不能包含凭据、查询参数或片段")
    if parsed.path not in {"", "/"}:
        raise ConfigError("可信来源不能包含路径")
    try:
        port = parsed.port
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError) as exc:
        raise ConfigError("可信来源的主机名或端口无效") from exc
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if parsed.scheme == "https" else 80
    port_suffix = f":{port}" if port and port != default_port else ""
    return f"{parsed.scheme}://{host}{port_suffix}"


def parse_trusted_origins(value: str) -> frozenset[str]:
    return frozenset(
        normalize_http_origin(item)
        for item in value.split(",")
        if item.strip()
    )


def validate_keepalive_url(value: str) -> str:
    """Validate a user-configured HTTP(S) target used to generate tunnel traffic."""
    candidate = value.strip()
    if not candidate:
        raise ConfigError("启用网址保活时必须填写保活网址")
    if any(character.isspace() for character in candidate):
        raise ConfigError("保活网址不能包含空白字符")
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigError("保活网址必须是有效的 HTTP 或 HTTPS 地址")
    if parsed.username or parsed.password:
        raise ConfigError("请勿在保活网址中包含用户名或密码")
    try:
        parsed.port
    except ValueError as exc:
        raise ConfigError("保活网址端口无效") from exc
    return candidate


def resolve_vpn_gateway(server: str, disable_ipv6: bool = True) -> tuple[str, str] | None:
    """Resolve a gateway before tunnel DNS is enabled so OpenConnect can pin the result."""
    parsed = urlparse(validate_server(server))
    host = parsed.hostname
    assert host is not None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass

    family = socket.AF_INET if disable_ipv6 else socket.AF_UNSPEC
    try:
        addresses = socket.getaddrinfo(
            host,
            parsed.port or 443,
            family=family,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ConfigError(f"无法使用连接前的系统 DNS 解析 VPN 网关 {host}: {exc}") from exc
    for _, _, _, _, sockaddr in addresses:
        if sockaddr and sockaddr[0]:
            return host, str(sockaddr[0])
    raise ConfigError(f"连接前的系统 DNS 没有返回 VPN 网关 {host} 的地址")


def _normalize_network_list(value: Any, label: str) -> list[str]:
    if value is None:
        items: list[Any] = []
    elif isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, list):
        items = value
    else:
        raise ConfigError(f"{label}必须是 CIDR 字符串数组")
    if len(items) > MAX_MANUAL_ROUTES:
        raise ConfigError(f"{label}最多允许 {MAX_MANUAL_ROUTES} 条")

    networks: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise ConfigError(f"{label}[{index}] 必须是 CIDR 字符串")
        candidate = item.strip()
        if not candidate:
            continue
        try:
            network = ipaddress.ip_network(candidate, strict=False).with_prefixlen
        except ValueError as exc:
            raise ConfigError(f"{label}[{index}] 不是有效 CIDR: {candidate}") from exc
        if network not in seen:
            seen.add(network)
            networks.append(network)
    return networks


def _normalize_dns_servers(value: Any) -> list[str]:
    if value is None:
        items: list[Any] = []
    elif isinstance(value, str):
        items = value.replace(",", "\n").splitlines()
    elif isinstance(value, list):
        items = value
    else:
        raise ConfigError("DNS 服务器必须是 IP 地址数组")
    servers: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise ConfigError(f"DNS 服务器[{index}] 必须是 IP 地址")
        candidate = item.strip()
        if not candidate:
            continue
        try:
            address = ipaddress.ip_address(candidate).compressed
        except ValueError as exc:
            raise ConfigError(f"DNS 服务器[{index}] 不是有效 IP 地址: {candidate}") from exc
        if address not in servers:
            servers.append(address)
        if len(servers) > 3:
            raise ConfigError("最多允许配置 3 个 DNS 服务器")
    return servers


def normalize_vpn_route_config(config: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(config)
    mode = str(result.get("route_mode", "all")).strip().lower()
    if mode not in VPN_ROUTE_MODES:
        raise ConfigError("路由模式必须是 all、vpn 或 manual")
    result["route_mode"] = mode
    result["manual_routes"] = _normalize_network_list(result.get("manual_routes"), "手动包含网段")
    result["manual_exclude_routes"] = _normalize_network_list(
        result.get("manual_exclude_routes"), "手动排除网段"
    )
    if mode == "manual" and not result["manual_routes"]:
        raise ConfigError("手动路由模式至少需要一个包含网段")

    dns_mode = str(result.get("dns_mode", "system")).strip().lower()
    if dns_mode not in VPN_DNS_MODES:
        raise ConfigError("全局 DNS 模式必须是 system、vpn 或 manual")
    result["dns_mode"] = dns_mode
    result["dns_servers"] = _normalize_dns_servers(result.get("dns_servers"))
    if dns_mode == "manual" and not result["dns_servers"]:
        raise ConfigError("手动 DNS 模式至少需要一个 DNS 服务器 IP")

    result["auto_reconnect"] = bool(result.get("auto_reconnect", True))
    try:
        reconnect_interval = int(result.get("auto_reconnect_interval", 10))
    except (TypeError, ValueError) as exc:
        raise ConfigError("自动重连间隔必须是整数") from exc
    if reconnect_interval < 1 or reconnect_interval > 3600:
        raise ConfigError("自动重连间隔必须在 1 到 3600 秒之间")
    result["auto_reconnect_interval"] = reconnect_interval

    result["keepalive_enabled"] = bool(result.get("keepalive_enabled", False))
    keepalive_url = str(result.get("keepalive_url", "")).strip()
    if result["keepalive_enabled"]:
        keepalive_url = validate_keepalive_url(keepalive_url)
    result["keepalive_url"] = keepalive_url
    try:
        keepalive_interval = int(result.get("keepalive_interval", 300))
    except (TypeError, ValueError) as exc:
        raise ConfigError("保活间隔必须是整数") from exc
    if keepalive_interval < 10 or keepalive_interval > 86400:
        raise ConfigError("保活间隔必须在 10 到 86400 秒之间")
    result["keepalive_interval"] = keepalive_interval
    return result


def perform_keepalive_request(
    url: str, mark: int = XRAY_MARK, timeout: float = 10, interface: str = "tun0"
) -> int:
    """Send a small HTTP request on a marked socket bound to the VPN interface."""
    parsed = urlparse(validate_keepalive_url(url))
    host = parsed.hostname
    assert host is not None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"
    try:
        request_target = target.encode("ascii")
        address_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ConfigError("保活网址路径必须使用 URL 编码") from exc
    host_header = f"[{address_host}]" if ":" in address_host else address_host
    if parsed.port:
        host_header += f":{port}"

    last_error: OSError | None = None
    for family, socktype, proto, _, sockaddr in socket.getaddrinfo(
        address_host, port, type=socket.SOCK_STREAM
    ):
        raw_socket = socket.socket(family, socktype, proto)
        try:
            raw_socket.settimeout(timeout)
            raw_socket.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_MARK", 36), mark)
            raw_socket.setsockopt(
                socket.SOL_SOCKET,
                getattr(socket, "SO_BINDTODEVICE", 25),
                interface.encode("ascii") + b"\0",
            )
            raw_socket.connect(sockaddr)
            connection: socket.socket
            if parsed.scheme == "https":
                connection = ssl.create_default_context().wrap_socket(
                    raw_socket, server_hostname=address_host
                )
            else:
                connection = raw_socket
            try:
                request = (
                    b"GET " + request_target + b" HTTP/1.1\r\n"
                    + f"Host: {host_header}\r\n".encode("ascii")
                    + b"User-Agent: proxy2openconnect-keepalive\r\n"
                    + b"Range: bytes=0-0\r\nConnection: close\r\n\r\n"
                )
                connection.sendall(request)
                response_line = b""
                while b"\n" not in response_line and len(response_line) < 4096:
                    chunk = connection.recv(1)
                    if not chunk:
                        break
                    response_line += chunk
            finally:
                connection.close()
            match = re.match(rb"HTTP/\d(?:\.\d)?\s+(\d{3})", response_line)
            if not match:
                raise OSError("保活网址返回了无效的 HTTP 响应")
            return int(match.group(1))
        except OSError as exc:
            last_error = exc
            raw_socket.close()
    raise OSError(f"无法连接保活网址: {last_error or '没有可用地址'}")


def vpn_route_environment(config: dict[str, Any]) -> dict[str, str]:
    normalized = normalize_vpn_route_config(config)
    return {
        "XRAY_VPN_ROUTE_MODE": normalized["route_mode"],
        "XRAY_VPN_MANUAL_ROUTES": "\n".join(normalized["manual_routes"]),
        "XRAY_VPN_MANUAL_EXCLUDE_ROUTES": "\n".join(normalized["manual_exclude_routes"]),
        "XRAY_VPN_DNS_MODE": normalized["dns_mode"],
        "XRAY_VPN_DNS_SERVERS": "\n".join(normalized["dns_servers"]),
    }


def _path_option(value: str, label: str) -> str:
    if not value:
        return ""
    path = Path(value)
    if not path.is_absolute() or not str(path).startswith("/data/"):
        raise ConfigError(f"{label}必须使用 /data/ 下的绝对路径")
    return str(path)


ALLOWED_EXTRA_ARGS = {
    "--allow-insecure-crypto", "--base-mtu", "--compression", "--deflate", "--force-dpd",
    "--local-hostname", "--mtu", "--no-deflate", "--no-external-auth", "--no-http-keepalive",
    "--no-xmlpost", "--os", "--passtos", "--pfs", "--queue-len", "--tcp-keepalive",
    "--version-string",
}


def build_openconnect_command(config: dict[str, Any]) -> list[str]:
    config = normalize_vpn_route_config(config)
    server = validate_server(str(config.get("server", "")))
    username = str(config.get("username", "")).strip()
    if not username:
        raise ConfigError("请填写 VPN 用户名")

    command = [
        OPENCONNECT_BINARY,
        "--protocol=anyconnect",
        "--interface=tun0",
        f"--script={VPN_SCRIPT}",
        "--passwd-on-stdin",
        "--non-inter",
        "--timestamp",
        f"--user={username}",
    ]
    options = {
        "authgroup": "--authgroup",
        "servercert": "--servercert",
        "useragent": "--useragent",
    }
    for key, flag in options.items():
        value = str(config.get(key, "")).strip()
        if value:
            command.append(f"{flag}={value}")

    path_options = {
        "certificate": ("--certificate", "客户端证书"),
        "sslkey": ("--sslkey", "证书私钥"),
        "cafile": ("--cafile", "CA 文件"),
    }
    for key, (flag, label) in path_options.items():
        value = _path_option(str(config.get(key, "")).strip(), label)
        if value:
            command.append(f"{flag}={value}")

    if config.get("no_dtls"):
        command.append("--no-dtls")
    if config.get("disable_ipv6", True):
        command.append("--disable-ipv6")
    reconnect_timeout = int(config.get("reconnect_timeout", 300))
    if reconnect_timeout < 0 or reconnect_timeout > 86400:
        raise ConfigError("自动重连时间必须在 0 到 86400 秒之间")
    command.append(f"--reconnect-timeout={reconnect_timeout}")

    extra_args = config.get("extra_args", [])
    if isinstance(extra_args, str):
        extra_args = shlex.split(extra_args)
    if not isinstance(extra_args, list) or any(not isinstance(item, str) for item in extra_args):
        raise ConfigError("extra_args 必须是字符串数组")
    for item in extra_args:
        if not item.startswith("--"):
            raise ConfigError("额外参数必须以 -- 开头")
        option = item.split("=", 1)[0]
        if option not in ALLOWED_EXTRA_ARGS:
            raise ConfigError(f"额外参数 {option} 不在安全允许列表中")
        command.append(item)
    command.append(server)
    return command


@dataclass
class ServiceProcess:
    name: str
    process: subprocess.Popen[str] | None = None
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=500))
    started_at: float | None = None
    last_exit_code: int | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None


class ProcessManager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.services = {
            "vpn": ServiceProcess("vpn"),
            "xray": ServiceProcess("xray"),
        }
        self._vpn_requested = False
        self._vpn_password = ""
        self._vpn_otp = ""
        self._vpn_requires_otp = False
        self._vpn_ever_connected = False
        self._vpn_reconnect_attempts = 0
        self._vpn_cancel_event = threading.Event()
        self._vpn_reconnect_pending = False
        self._keepalive_wakeup = threading.Event()
        self._last_keepalive_at: float | None = None
        self._last_keepalive_ok: bool | None = None
        self._last_keepalive_error: str | None = None
        threading.Thread(target=self._keepalive_loop, daemon=True).start()

    def _append(self, service: ServiceProcess, line: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        service.logs.append(f"{timestamp}  {line.rstrip()}")

    def _read_output(self, service: ServiceProcess, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            with self.lock:
                self._append(service, line)
                if service.name == "vpn" and (
                    VPN_CONNECTED.exists() or "Configured as " in line
                ):
                    self._vpn_ever_connected = True
                    self._vpn_otp = ""
                    self._vpn_reconnect_attempts = 0
        code = process.wait()
        with self.lock:
            is_current = service.process is process
            if is_current:
                service.last_exit_code = code
                self._append(service, f"进程已退出，代码 {code}")
                if service.name == "vpn":
                    if VPN_CONNECTED.exists():
                        self._vpn_ever_connected = True
                        # An OTP is normally single-use and must never be replayed.
                        self._vpn_otp = ""
                        self._vpn_reconnect_attempts = 0
                    self.ensure_direct_fallback()
                    self._keepalive_wakeup.set()
                    self._schedule_vpn_reconnect()

    def _spawn(
        self,
        name: str,
        command: list[str],
        stdin_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        service = self.services[name]
        if service.running:
            raise ConfigError(f"{name} 已在运行")
        self._append(service, "正在启动…")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=env,
        )
        service.process = process
        service.started_at = time.time()
        service.last_exit_code = None
        threading.Thread(target=self._read_output, args=(service, process), daemon=True).start()
        if stdin_text is not None and process.stdin is not None:
            process.stdin.write(stdin_text)
            if not stdin_text.endswith("\n"):
                process.stdin.write("\n")
            process.stdin.flush()
            process.stdin.close()

    def start_vpn(self, password: str = "", otp: str = "") -> None:
        with self.lock:
            config = normalize_vpn_route_config(read_json(VPN_CONFIG))
            secret = password or str(config.get("password", ""))
            if not secret:
                raise ConfigError("请输入 VPN 密码")
            if self.services["vpn"].running:
                raise ConfigError("vpn 已在运行")
            self._vpn_cancel_event.set()
            self._vpn_cancel_event = threading.Event()
            self._vpn_requested = True
            self._vpn_password = secret
            self._vpn_otp = otp
            self._vpn_requires_otp = bool(otp)
            self._vpn_ever_connected = False
            self._vpn_reconnect_attempts = 0
            self._vpn_reconnect_pending = False
            self._start_vpn_attempt(config, secret, otp)
            self._keepalive_wakeup.set()

    def _start_vpn_attempt(self, config: dict[str, Any], secret: str, otp: str = "") -> None:
        # Restore the pre-tunnel resolver before looking up the public VPN gateway.
        self.ensure_direct_fallback()
        command = build_openconnect_command(config)
        gateway = resolve_vpn_gateway(
            command[-1], disable_ipv6=bool(config.get("disable_ipv6", True))
        )
        if gateway:
            host, address = gateway
            command.insert(-1, f"--resolve={host}:{address}")
            self._append(self.services["vpn"], f"VPN 网关已通过连接前 DNS 解析并固定为 {address}")
        stdin_text = secret + (f"\n{otp}" if otp else "")
        process_env = os.environ.copy()
        process_env.update(vpn_route_environment(config))
        self._spawn("vpn", command, stdin_text, process_env)

    def _schedule_vpn_reconnect(self) -> None:
        if not self._vpn_requested or self._vpn_reconnect_pending:
            return
        try:
            config = normalize_vpn_route_config(read_json(VPN_CONFIG))
        except ConfigError as exc:
            self._append(self.services["vpn"], f"自动重连已停止: {exc}")
            return
        if not config["auto_reconnect"] or not self._vpn_ever_connected:
            return
        if self._vpn_requires_otp:
            self._append(self.services["vpn"], "会话需要新的 OTP，已跳过自动重连")
            return
        if self._vpn_reconnect_attempts >= MAX_AUTO_RECONNECT_ATTEMPTS:
            self._append(
                self.services["vpn"],
                f"自动重连连续失败 {MAX_AUTO_RECONNECT_ATTEMPTS} 次，已停止重试",
            )
            return
        interval = config["auto_reconnect_interval"]
        cancel_event = self._vpn_cancel_event
        self._vpn_reconnect_pending = True
        self._append(self.services["vpn"], f"将在 {interval} 秒后自动重连")

        def reconnect() -> None:
            if cancel_event.wait(interval):
                return
            with self.lock:
                self._vpn_reconnect_pending = False
                if cancel_event is not self._vpn_cancel_event or not self._vpn_requested:
                    return
                if self.services["vpn"].running:
                    return
                try:
                    current = normalize_vpn_route_config(read_json(VPN_CONFIG))
                    if not current["auto_reconnect"]:
                        self._append(self.services["vpn"], "自动重连已在配置中关闭")
                        return
                    self._vpn_reconnect_attempts += 1
                    self._append(
                        self.services["vpn"],
                        f"正在自动重连（{self._vpn_reconnect_attempts}/{MAX_AUTO_RECONNECT_ATTEMPTS}）…",
                    )
                    self._start_vpn_attempt(current, self._vpn_password, self._vpn_otp)
                except Exception as exc:
                    self._append(self.services["vpn"], f"自动重连启动失败: {exc}")
                    self._schedule_vpn_reconnect()

        threading.Thread(target=reconnect, daemon=True).start()

    def notify_vpn_config_changed(self) -> None:
        self._keepalive_wakeup.set()

    def _keepalive_loop(self) -> None:
        signature: tuple[str, int] | None = None
        next_request_at: float | None = None
        while True:
            try:
                config = normalize_vpn_route_config(read_json(VPN_CONFIG))
                enabled = config["keepalive_enabled"]
                current_signature = (config["keepalive_url"], config["keepalive_interval"])
            except ConfigError:
                enabled = False
                current_signature = ("", 300)

            connected = self.services["vpn"].running and VPN_CONNECTED.exists()
            now = time.monotonic()
            if not enabled or not connected:
                signature = None
                next_request_at = None
                self._keepalive_wakeup.wait(5)
                self._keepalive_wakeup.clear()
                continue
            if signature != current_signature or next_request_at is None:
                signature = current_signature
                next_request_at = now + current_signature[1]
            remaining = max(0.0, next_request_at - now)
            if self._keepalive_wakeup.wait(remaining):
                self._keepalive_wakeup.clear()
                continue

            url, interval = current_signature
            try:
                status = perform_keepalive_request(url)
                with self.lock:
                    self._last_keepalive_at = time.time()
                    self._last_keepalive_ok = True
                    self._last_keepalive_error = None
                    self._append(self.services["vpn"], f"网址保活成功（HTTP {status}）")
            except Exception as exc:
                with self.lock:
                    self._last_keepalive_at = time.time()
                    self._last_keepalive_ok = False
                    self._last_keepalive_error = str(exc)
                    self._append(self.services["vpn"], f"网址保活失败: {exc}")
            next_request_at = time.monotonic() + interval

    def start_xray(self) -> None:
        with self.lock:
            config = read_json(XRAY_CONFIG)
            validate_xray_shape(config)
            effective = effective_xray_config(config)
            atomic_write_json(RUNTIME_CONFIG, effective)
            self.validate_xray(effective)
            self._spawn("xray", [XRAY_BINARY, "run", "-config", str(RUNTIME_CONFIG)])

    def validate_xray(self, config: dict[str, Any]) -> str:
        validate_xray_shape(config)
        if not Path(XRAY_BINARY).exists():
            return "结构检查通过（当前环境未安装 Xray，跳过核心检查）"
        fd, name = tempfile.mkstemp(suffix=".json", prefix="xray-check-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(effective_xray_config(config), handle, ensure_ascii=False)
            result = subprocess.run(
                [XRAY_BINARY, "run", "-test", "-config", name],
                capture_output=True,
                text=True,
                timeout=15,
            )
            output = (result.stdout + result.stderr).strip()
            if result.returncode != 0:
                raise ConfigError(output or "Xray 核心校验失败")
            return output or "Xray 核心校验通过"
        finally:
            os.unlink(name)

    def stop(self, name: str) -> None:
        with self.lock:
            service = self.services[name]
            if name == "vpn":
                self._vpn_requested = False
                self._vpn_password = ""
                self._vpn_otp = ""
                self._vpn_requires_otp = False
                self._vpn_reconnect_attempts = 0
                self._vpn_reconnect_pending = False
                self._vpn_cancel_event.set()
                self._keepalive_wakeup.set()
            process = service.process
            if not process or process.poll() is not None:
                if name == "vpn":
                    self.ensure_direct_fallback()
                return
            self._append(service, "正在停止…")
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        with self.lock:
            if name == "vpn" and service.process is process:
                self.ensure_direct_fallback()

    def restart_xray(self) -> None:
        self.stop("xray")
        self.start_xray()

    def ensure_direct_fallback(self) -> None:
        script = "/opt/proxy2openconnect/scripts/route-guard.sh"
        if Path(script).exists() and shutil.which("ip"):
            subprocess.run([script], capture_output=True, text=True, timeout=10, check=False)

    def logs(self, name: str) -> list[str]:
        with self.lock:
            return list(self.services[name].logs)

    def certificate_candidate(self) -> dict[str, str] | None:
        with self.lock:
            candidate = extract_certificate_candidate(list(self.services["vpn"].logs))
            if not candidate:
                return None
            try:
                config = read_json(VPN_CONFIG)
            except ConfigError:
                return candidate
            if str(config.get("servercert", "")).strip() == candidate["pin"]:
                return None
            return candidate

    def trust_certificate_candidate(self) -> dict[str, str]:
        with self.lock:
            if self.services["vpn"].running:
                raise ConfigError("请先断开当前 VPN 连接")
            candidate = extract_certificate_candidate(list(self.services["vpn"].logs))
            if not candidate:
                raise ConfigError("没有检测到可信任的服务器证书指纹")
            config = read_json(VPN_CONFIG)
            expected_host = urlparse(validate_server(str(config.get("server", "")))).hostname
            if not expected_host or candidate["host"] != expected_host.lower():
                raise ConfigError("检测到的证书主机与当前 VPN 配置不一致")
            config["servercert"] = candidate["pin"]
            atomic_write_json(VPN_CONFIG, config)
            self._append(self.services["vpn"], f"已固定 {candidate['host']} 的服务器证书公钥指纹")
            return candidate

    def status(self) -> dict[str, Any]:
        with self.lock:
            services: dict[str, Any] = {}
            for name, service in self.services.items():
                services[name] = {
                    "running": service.running,
                    "pid": service.process.pid if service.running and service.process else None,
                    "started_at": service.started_at,
                    "last_exit_code": service.last_exit_code,
                }
            services["vpn"]["reconnect_pending"] = self._vpn_reconnect_pending
            keepalive = {
                "last_at": self._last_keepalive_at,
                "ok": self._last_keepalive_ok,
                "error": self._last_keepalive_error,
            }
        vpn_ip = None
        if VPN_CONNECTED.exists():
            vpn_ip = VPN_CONNECTED.read_text(encoding="utf-8").strip()
        vpn_routes: list[str] = []
        if VPN_ROUTES.exists():
            vpn_routes = [
                line.strip()
                for line in VPN_ROUTES.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        active_dns: list[str] = []
        if DNS_ACTIVE.exists():
            active_dns = [
                line.strip()
                for line in DNS_ACTIVE.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        return {
            "services": services,
            "vpn_connected": bool(vpn_ip and services["vpn"]["running"]),
            "vpn_ip": vpn_ip,
            "vpn_routes": vpn_routes,
            "active_dns": active_dns,
            "route_table": ROUTE_TABLE,
            "mark": XRAY_MARK,
            "certificate_candidate": self.certificate_candidate(),
            "keepalive": keepalive,
        }


manager = ProcessManager()
