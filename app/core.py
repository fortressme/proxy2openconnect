from __future__ import annotations

import copy
import base64
import hashlib
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
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
STATISTICS_DIR = DATA_DIR / "statistics"
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
XRAY_ACCESS_TARGET_PATTERN = re.compile(
    r"\baccepted\s+(?P<network>tcp|udp):(?P<endpoint>\[[^]]+]:\d+|\S+:\d+)"
    r"(?:\s+\((?P<domain>[^()\s]+)\))?\s+\[(?P<route>[^]]*)]",
    re.IGNORECASE,
)
XRAY_ACCESS_DOMAIN_PATTERN = re.compile(r"\bDomain:\s*([^,\s]+)", re.IGNORECASE)


def extract_certificate_candidate(lines: list[str]) -> dict[str, str] | None:
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


def xray_uses_default_password(config: dict[str, Any]) -> bool:
    def contains_placeholder(value: Any) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in {"pass", "password"} and item == "change-me":
                    return True
                if contains_placeholder(item):
                    return True
        elif isinstance(value, list):
            return any(contains_placeholder(item) for item in value)
        return False

    return contains_placeholder(config.get("inbounds", []))


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

    try:
        retention_days = int(result.get("statistics_retention_days", 30))
    except (TypeError, ValueError) as exc:
        raise ConfigError("目标统计保留天数必须是整数") from exc
    if retention_days < 1 or retention_days > 365:
        raise ConfigError("目标统计保留天数必须在 1 到 365 天之间")
    result["statistics_retention_days"] = retention_days
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


def _read_interface_statistics(interface: str = "tun0") -> dict[str, int | bool]:
    statistics_dir = Path("/sys/class/net") / interface / "statistics"
    result: dict[str, int | bool] = {
        "available": statistics_dir.exists(),
        "rx_bytes": 0,
        "tx_bytes": 0,
        "rx_packets": 0,
        "tx_packets": 0,
        "rx_errors": 0,
        "tx_errors": 0,
    }
    if not result["available"]:
        return result
    for key in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors"):
        try:
            result[key] = int((statistics_dir / key).read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            result[key] = 0
    return result


XRAY_PROTOCOL_LABELS = {
    "dokodemo-door": "DOKODEMO",
    "http": "HTTP",
    "shadowsocks": "SHADOWSOCKS",
    "socks": "SOCKS",
    "trojan": "TROJAN",
    "vless": "VLESS",
    "vmess": "VMESS",
    "wireguard": "WIREGUARD",
}


def _xray_inbound_overview(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if config is None:
        try:
            config = read_json(XRAY_CONFIG)
        except ConfigError:
            return []
    overview: list[dict[str, Any]] = []
    for inbound in config.get("inbounds", []):
        if not isinstance(inbound, dict):
            continue
        protocol = str(inbound.get("protocol", "")).strip().lower()
        if not protocol:
            continue
        port = inbound.get("port")
        if not isinstance(port, (int, str)) or isinstance(port, bool):
            port = None
        elif isinstance(port, str):
            port = port.strip() or None
        overview.append(
            {
                "protocol": protocol,
                "label": XRAY_PROTOCOL_LABELS.get(
                    protocol, protocol.replace("-", " ").upper()
                ),
                "port": port,
                "listen": str(inbound.get("listen", "")).strip(),
                "tag": str(inbound.get("tag", "")).strip(),
            }
        )
    return overview


def _xray_inbound_ports() -> list[int]:
    ports: set[int] = set()
    for inbound in _xray_inbound_overview():
        port = inbound["port"]
        if isinstance(port, int) and 0 < port <= 65535:
            ports.add(port)
        elif isinstance(port, str) and port.isdigit() and 0 < int(port) <= 65535:
            ports.add(int(port))
    return sorted(ports)


def _decode_proc_endpoint(value: str, ipv6: bool) -> tuple[str, int]:
    address_hex, port_hex = value.split(":", 1)
    if ipv6:
        packed = b"".join(
            bytes.fromhex(address_hex[offset : offset + 8])[::-1]
            for offset in range(0, 32, 8)
        )
        address = str(ipaddress.IPv6Address(packed))
    else:
        address = str(ipaddress.IPv4Address(bytes.fromhex(address_hex)[::-1]))
    return address, int(port_hex, 16)


def _process_socket_inodes(pid: int | None) -> set[str]:
    if not pid:
        return set()
    inodes: set[str] = set()
    try:
        descriptors = (Path("/proc") / str(pid) / "fd").iterdir()
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            match = re.fullmatch(r"socket:\[(\d+)]", target)
            if match:
                inodes.add(match.group(1))
    except OSError:
        pass
    return inodes


def _format_endpoint(address: str, port: int) -> str:
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def _split_endpoint(endpoint: str) -> tuple[str, int] | None:
    if endpoint.startswith("["):
        closing = endpoint.find("]:")
        if closing < 0:
            return None
        host, port_text = endpoint[1:closing], endpoint[closing + 2 :]
    else:
        host, separator, port_text = endpoint.rpartition(":")
        if not separator:
            return None
    try:
        port = int(port_text)
    except ValueError:
        return None
    if not host or not 0 < port <= 65535:
        return None
    return host, port


def _parse_xray_access_target(line: str) -> dict[str, Any] | None:
    match = XRAY_ACCESS_TARGET_PATTERN.search(line)
    if not match or match.group("network").lower() != "tcp":
        return None
    parsed = _split_endpoint(match.group("endpoint"))
    if not parsed:
        return None
    host, port = parsed
    trailing_domain = XRAY_ACCESS_DOMAIN_PATTERN.search(line)
    domain_hint = (
        match.group("domain")
        or (trailing_domain.group(1) if trailing_domain else "")
    ).strip().lower().rstrip(".")
    try:
        address = str(ipaddress.ip_address(host))
        domain = domain_hint or None
    except ValueError:
        address = None
        domain = host.lower().rstrip(".")
    route = match.group("route").strip()
    route_tags = [
        tag.strip()
        for tag in re.split(r"\s*(?:>>|->)\s*", route)
        if tag.strip()
    ]
    outbound_tag = route_tags[-1] if route_tags else None
    return {
        "domain": domain,
        "address": address,
        "port": port,
        "outbound_tag": outbound_tag,
        "observed_at": time.monotonic(),
    }


def _xray_access_log_path(config: dict[str, Any]) -> Path | None:
    log = config.get("log")
    if not isinstance(log, dict):
        return None
    value = log.get("access")
    if not isinstance(value, str) or not value or value.lower() == "none":
        return None
    path = Path(value)
    return path if path.is_absolute() else Path.cwd() / path


def _xray_tcp_connections(pid: int | None) -> list[dict[str, Any]]:
    socket_inodes = _process_socket_inodes(pid)
    connections: list[dict[str, Any]] = []
    if not socket_inodes:
        return connections
    for filename, ipv6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        try:
            lines = Path(filename).read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "01" or fields[9] not in socket_inodes:
                continue
            try:
                local_address, local_port = _decode_proc_endpoint(fields[1], ipv6)
                remote_address, remote_port = _decode_proc_endpoint(fields[2], ipv6)
            except (ValueError, IndexError):
                continue
            connections.append(
                {
                    "inode": fields[9],
                    "local_address": local_address,
                    "local_port": local_port,
                    "remote_address": remote_address,
                    "remote_port": remote_port,
                }
            )
    return connections


def _tunnel_interface_addresses(interface: str = "tun0") -> set[str]:
    addresses: set[str] = set()
    try:
        connected_address = VPN_CONNECTED.read_text(encoding="utf-8").strip()
        addresses.add(str(ipaddress.ip_address(connected_address)))
    except (OSError, ValueError):
        pass
    try:
        result = subprocess.run(
            ["ip", "-j", "address", "show", "dev", interface],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        interfaces = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return addresses
    for item in interfaces:
        for address in item.get("addr_info", []):
            value = address.get("local")
            if isinstance(value, str):
                try:
                    addresses.add(str(ipaddress.ip_address(value)))
                except ValueError:
                    continue
    return addresses


def _classify_xray_targets(
    connections: list[dict[str, Any]],
    target_names: dict[str, dict[str, Any]],
    tunnel_addresses: set[str],
    vpn_connected: bool,
) -> dict[str, dict[str, Any]]:
    classified: dict[str, dict[str, Any]] = {}
    for connection in connections:
        matched = connection["inode"] in target_names
        target = dict(target_names.get(connection["inode"], {}))
        if tunnel_addresses:
            via_vpn = connection["local_address"] in tunnel_addresses
            route = "vpn" if via_vpn else "direct"
        elif not vpn_connected:
            via_vpn = False
            route = "direct"
        else:
            via_vpn = None
            route = "unknown"
        target.update(
            {
                "log_matched": matched,
                "route": route,
                "via_vpn": via_vpn,
            }
        )
        classified[connection["inode"]] = target
    return classified


def _summarize_xray_connections(
    connections: list[dict[str, Any]],
    inbound_ports: list[int] | None = None,
    target_names: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    inbound_ports = _xray_inbound_ports() if inbound_ports is None else inbound_ports
    target_names = target_names or {}
    client_counts: Counter[str] = Counter()
    target_counts: Counter[tuple[str, int, str, str]] = Counter()
    target_addresses: dict[tuple[str, int, str, str], Counter[str]] = {}
    target_domains: dict[tuple[str, int, str, str], str | None] = {}
    for connection in connections:
        if connection["local_port"] in inbound_ports:
            client_counts[connection["remote_address"]] += 1
        else:
            metadata = target_names.get(connection["inode"], {})
            domain = metadata.get("domain")
            primary = domain or connection["remote_address"]
            route = metadata.get("route", "unknown")
            outbound_tag = metadata.get("outbound_tag") or ""
            key = (primary, connection["remote_port"], route, outbound_tag)
            target_counts[key] += 1
            target_addresses.setdefault(key, Counter())[connection["remote_address"]] += 1
            target_domains[key] = domain

    clients = [
        {"address": address, "connections": count}
        for address, count in client_counts.most_common(12)
    ]
    targets = []
    for (primary, port, route, outbound_tag), count in target_counts.most_common(12):
        key = (primary, port, route, outbound_tag)
        address_counts = target_addresses[key]
        addresses = [address for address, _ in address_counts.most_common(4)]
        address = addresses[0]
        domain = target_domains[key]
        try:
            scope = "public" if ipaddress.ip_address(address).is_global else "private"
        except ValueError:
            scope = "unknown"
        targets.append(
            {
                "address": address,
                "addresses": addresses,
                "domain": domain,
                "port": port,
                "endpoint": _format_endpoint(domain or address, port),
                "connections": count,
                "scope": scope,
                "route": route,
                "via_vpn": None if route == "unknown" else route == "vpn",
                "outbound_tag": outbound_tag or None,
            }
        )

    return {
        # Retain legacy client fields for API compatibility.
        "active": sum(client_counts.values()),
        "unique_addresses": len(client_counts),
        "inbound_ports": inbound_ports,
        "addresses": clients,
        "clients": {
            "active": sum(client_counts.values()),
            "unique_addresses": len(client_counts),
            "addresses": clients,
        },
        "targets": {
            "active": sum(target_counts.values()),
            "vpn_active": sum(
                count
                for (_, _, route, _), count in target_counts.items()
                if route == "vpn"
            ),
            "direct_active": sum(
                count
                for (_, _, route, _), count in target_counts.items()
                if route == "direct"
            ),
            "unknown_active": sum(
                count
                for (_, _, route, _), count in target_counts.items()
                if route == "unknown"
            ),
            "unique_addresses": len({primary for primary, _, _, _ in target_counts}),
            "unique_endpoints": len(
                {(primary, port) for primary, port, _, _ in target_counts}
            ),
            "addresses": targets,
        },
    }


def _vpn_statistics_profile(config: dict[str, Any]) -> dict[str, str] | None:
    try:
        parsed = urlparse(validate_server(str(config.get("server", ""))))
    except ConfigError:
        return None
    hostname = parsed.hostname
    if not hostname:
        return None
    default_port = 443
    server = hostname.lower()
    if parsed.port and parsed.port != default_port:
        server = f"{server}:{parsed.port}"
    identity = json.dumps(
        [server, str(config.get("username", "")).strip(), str(config.get("authgroup", "")).strip()],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "id": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
        "server": server,
    }


class TargetHistoryStore:
    """Persist aggregated target connection counts in one JSONL log per local day."""

    def __init__(self, directory: Path = STATISTICS_DIR) -> None:
        self.directory = directory
        self.lock = threading.RLock()
        self.io_lock = threading.RLock()
        self.pending: Counter[
            tuple[str, str, str, str, str, str, str, int]
        ] = Counter()
        self.last_seen: dict[
            tuple[str, str, str, str, str, str, str, int], float
        ] = {}

    def record(
        self,
        profile: dict[str, str],
        address: str,
        port: int,
        domain: str | None = None,
        route: str = "unknown",
        outbound_tag: str | None = None,
    ) -> None:
        timestamp = time.time()
        day = datetime.fromtimestamp(timestamp).date().isoformat()
        key = (
            day,
            profile["id"],
            profile["server"],
            route,
            outbound_tag or "",
            domain or "",
            address,
            int(port),
        )
        with self.lock:
            self.pending[key] += 1
            self.last_seen[key] = timestamp

    def flush(self) -> None:
        with self.io_lock:
            with self.lock:
                if not self.pending:
                    return
                pending = self.pending
                last_seen = self.last_seen
                self.pending = Counter()
                self.last_seen = {}
            self.directory.mkdir(parents=True, exist_ok=True)
            grouped: dict[tuple[str, str], list[str]] = {}
            for key, count in pending.items():
                (
                    day,
                    profile_id,
                    server,
                    route,
                    outbound_tag,
                    domain,
                    address,
                    port,
                ) = key
                payload = {
                    "timestamp": last_seen[key],
                    "vpn_id": profile_id,
                    "vpn_server": server,
                    "address": address,
                    "domain": domain or None,
                    "port": port,
                    "route": route,
                    "via_vpn": None if route == "unknown" else route == "vpn",
                    "outbound_tag": outbound_tag or None,
                    "count": count,
                }
                grouped.setdefault((profile_id, day), []).append(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                )
            for (profile_id, day), lines in grouped.items():
                profile_directory = self.directory / profile_id
                profile_directory.mkdir(parents=True, exist_ok=True)
                path = profile_directory / f"targets-{day}.log"
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
                path.chmod(0o600)

    def cleanup(self, profile: dict[str, str] | None, retention_days: int) -> None:
        with self.io_lock:
            cutoff = date.today() - timedelta(days=retention_days - 1)
            if not profile:
                return
            profile_directory = self.directory / profile["id"]
            if not profile_directory.exists():
                return
            for path in profile_directory.glob("targets-????-??-??.log"):
                try:
                    log_day = date.fromisoformat(path.stem.removeprefix("targets-"))
                except ValueError:
                    continue
                if log_day < cutoff:
                    try:
                        path.unlink()
                    except OSError:
                        continue

    def summary(
        self, profile: dict[str, str] | None, retention_days: int
    ) -> dict[str, Any]:
        with self.io_lock:
            self.flush()
            self.cleanup(profile, retention_days)
            cutoff = date.today() - timedelta(days=retention_days - 1)
            counts: Counter[tuple[str, str, str, str, int]] = Counter()
            resolved_addresses: dict[
                tuple[str, str, str, str, int], Counter[str]
            ] = {}
            last_seen: dict[tuple[str, str, str, str, int], float] = {}
            active_days: dict[tuple[str, str, str, str, int], set[str]] = {}
            daily: Counter[str] = Counter()
            if profile:
                profile_directory = self.directory / profile["id"]
                for offset in range(retention_days):
                    day = cutoff + timedelta(days=offset)
                    path = profile_directory / f"targets-{day.isoformat()}.log"
                    try:
                        lines = path.read_text(encoding="utf-8").splitlines()
                    except OSError:
                        continue
                    for line in lines:
                        try:
                            entry = json.loads(line)
                            if entry.get("vpn_id") != profile["id"]:
                                continue
                            address = str(entry["address"])
                            domain = (
                                str(entry.get("domain") or "")
                                .strip()
                                .lower()
                                .rstrip(".")
                            )
                            port = int(entry["port"])
                            route = str(entry.get("route") or "unknown")
                            if route not in {"vpn", "direct", "unknown"}:
                                route = "unknown"
                            outbound_tag = str(entry.get("outbound_tag") or "")
                            count = max(0, int(entry["count"]))
                            timestamp = float(entry["timestamp"])
                        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                            continue
                        endpoint = (
                            route,
                            outbound_tag,
                            domain,
                            "" if domain else address,
                            port,
                        )
                        counts[endpoint] += count
                        resolved_addresses.setdefault(endpoint, Counter())[address] += count
                        last_seen[endpoint] = max(last_seen.get(endpoint, 0), timestamp)
                        active_days.setdefault(endpoint, set()).add(day.isoformat())
                        daily[day.isoformat()] += count
        targets = []
        for (
            route,
            outbound_tag,
            domain,
            fallback_address,
            port,
        ), count in counts.most_common(50):
            endpoint_key = (route, outbound_tag, domain, fallback_address, port)
            addresses = [
                address
                for address, _ in resolved_addresses[endpoint_key].most_common(4)
            ]
            address = addresses[0]
            try:
                scope = "public" if ipaddress.ip_address(address).is_global else "private"
            except ValueError:
                scope = "unknown"
            targets.append(
                {
                    "address": address,
                    "addresses": addresses,
                    "domain": domain or None,
                    "port": port,
                    "endpoint": _format_endpoint(domain or address, port),
                    "connections": count,
                    "last_seen": last_seen[endpoint_key],
                    "active_days": len(active_days[endpoint_key]),
                    "scope": scope,
                    "route": route,
                    "via_vpn": None if route == "unknown" else route == "vpn",
                    "outbound_tag": outbound_tag or None,
                }
            )
        return {
            "vpn": profile,
            "retention_days": retention_days,
            "period_start": cutoff.isoformat(),
            "total_connections": sum(counts.values()),
            "vpn_connections": sum(
                count
                for (route, _, _, _, _), count in counts.items()
                if route == "vpn"
            ),
            "direct_connections": sum(
                count
                for (route, _, _, _, _), count in counts.items()
                if route == "direct"
            ),
            "unknown_connections": sum(
                count
                for (route, _, _, _, _), count in counts.items()
                if route == "unknown"
            ),
            "unique_addresses": len(
                {domain or fallback for _, _, domain, fallback, _ in counts}
            ),
            "unique_endpoints": len(
                {
                    (domain or fallback, port)
                    for _, _, domain, fallback, port in counts
                }
            ),
            "active_days": len([value for value in daily.values() if value]),
            "daily": [
                {"date": day, "connections": daily[day]}
                for day in sorted(daily)
            ],
            "targets": targets,
        }


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
        self._vpn_reconnect_attempts_total = 0
        self._last_vpn_retry_at: float | None = None
        self._next_vpn_retry_at: float | None = None
        self._vpn_cancel_event = threading.Event()
        self._vpn_reconnect_pending = False
        self._last_traffic_sample: tuple[float, int, int] | None = None
        self._target_history = TargetHistoryStore()
        self._connection_statistics = _summarize_xray_connections([], [])
        self._statistics_xray_pid: int | None = None
        self._observed_outbound_inodes: set[str] = set()
        self._pending_history_connections: dict[str, tuple[float, dict[str, Any]]] = {}
        self._pending_xray_targets: deque[dict[str, Any]] = deque(maxlen=2048)
        self._socket_target_names: dict[str, dict[str, Any]] = {}
        self._tunnel_addresses: set[str] = set()
        self._next_tunnel_address_refresh = 0.0
        self._statistics_wakeup = threading.Event()
        self._keepalive_wakeup = threading.Event()
        self._last_keepalive_at: float | None = None
        self._last_keepalive_ok: bool | None = None
        self._last_keepalive_error: str | None = None
        threading.Thread(target=self._keepalive_loop, daemon=True).start()
        threading.Thread(target=self._statistics_loop, daemon=True).start()

    def _append(self, service: ServiceProcess, line: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        service.logs.append(f"{timestamp}  {line.rstrip()}")

    def _read_output(self, service: ServiceProcess, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            with self.lock:
                self._append(service, line)
                if service.name == "xray":
                    target = _parse_xray_access_target(line)
                    if target:
                        self._pending_xray_targets.append(target)
                if service.name == "vpn" and (
                    VPN_CONNECTED.exists() or "Configured as " in line
                ):
                    self._vpn_ever_connected = True
                    self._vpn_otp = ""
                    self._vpn_reconnect_attempts = 0
                    self._next_vpn_retry_at = None
        code = process.wait()
        with self.lock:
            is_current = service.process is process
            if is_current:
                service.last_exit_code = code
                self._append(service, f"进程已退出，代码 {code}")
                if service.name == "vpn":
                    if VPN_CONNECTED.exists():
                        self._vpn_ever_connected = True
                        # Never replay a single-use OTP.
                        self._vpn_otp = ""
                        self._vpn_reconnect_attempts = 0
                    self.ensure_direct_fallback()
                    self._keepalive_wakeup.set()
                    self._schedule_vpn_reconnect()

    def _read_xray_access_file(
        self, path: Path, process: subprocess.Popen[str], position: int
    ) -> None:
        while process.poll() is None:
            try:
                if path.stat().st_size < position:
                    position = 0
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(position)
                    lines = handle.readlines()
                    position = handle.tell()
            except OSError:
                lines = []
            if lines:
                with self.lock:
                    for line in lines:
                        target = _parse_xray_access_target(line)
                        if target:
                            self._pending_xray_targets.append(target)
            time.sleep(0.2)

    def _associate_xray_targets(
        self, connections: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        cutoff = time.monotonic() - 10
        while self._pending_xray_targets and self._pending_xray_targets[0]["observed_at"] < cutoff:
            self._pending_xray_targets.popleft()

        current_inodes = {connection["inode"] for connection in connections}
        self._socket_target_names = {
            inode: target
            for inode, target in self._socket_target_names.items()
            if inode in current_inodes
        }
        for connection in connections:
            inode = connection["inode"]
            if inode in self._socket_target_names:
                continue
            matched_index: int | None = None
            for index, target in enumerate(self._pending_xray_targets):
                if target["port"] != connection["remote_port"]:
                    continue
                if target["address"] and target["address"] != connection["remote_address"]:
                    continue
                matched_index = index
                break
            if matched_index is None:
                continue
            target = self._pending_xray_targets[matched_index]
            del self._pending_xray_targets[matched_index]
            self._socket_target_names[inode] = target
        return dict(self._socket_target_names)

    def _spawn(
        self,
        name: str,
        command: list[str],
        stdin_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[str]:
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
        self._statistics_wakeup.set()
        threading.Thread(target=self._read_output, args=(service, process), daemon=True).start()
        if stdin_text is not None and process.stdin is not None:
            process.stdin.write(stdin_text)
            if not stdin_text.endswith("\n"):
                process.stdin.write("\n")
            process.stdin.flush()
            process.stdin.close()
        return process

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
            self._vpn_reconnect_attempts_total = 0
            self._last_vpn_retry_at = None
            self._next_vpn_retry_at = None
            self._vpn_reconnect_pending = False
            self._start_vpn_attempt(config, secret, otp)
            self._keepalive_wakeup.set()

    def _start_vpn_attempt(self, config: dict[str, Any], secret: str, otp: str = "") -> None:
        # Restore pre-tunnel DNS before resolving the public gateway.
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
        self._next_vpn_retry_at = time.time() + interval
        self._append(self.services["vpn"], f"将在 {interval} 秒后自动重连")

        def reconnect() -> None:
            if cancel_event.wait(interval):
                return
            with self.lock:
                self._vpn_reconnect_pending = False
                self._next_vpn_retry_at = None
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
                    self._vpn_reconnect_attempts_total += 1
                    self._last_vpn_retry_at = time.time()
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
        self._statistics_wakeup.set()

    def _statistics_loop(self) -> None:
        next_flush_at = time.monotonic() + 10
        while True:
            with self.lock:
                xray_service = self.services["xray"]
                vpn_service = self.services["vpn"]
                xray_pid = xray_service.process.pid if xray_service.running and xray_service.process else None
                vpn_running = vpn_service.running
            inbound_ports = _xray_inbound_ports()
            connections = _xray_tcp_connections(xray_pid)
            outbound = [
                connection
                for connection in connections
                if connection["local_port"] not in inbound_ports
            ]
            current_inodes = {connection["inode"] for connection in outbound}
            connected = bool(vpn_running and VPN_CONNECTED.exists())
            now = time.monotonic()
            with self.lock:
                cached_tunnel_addresses = set(self._tunnel_addresses)
                next_tunnel_refresh = self._next_tunnel_address_refresh
            if now >= next_tunnel_refresh or (connected and not cached_tunnel_addresses):
                tunnel_addresses = _tunnel_interface_addresses() if connected else set()
                with self.lock:
                    self._tunnel_addresses = tunnel_addresses
                    self._next_tunnel_address_refresh = now + (
                        2 if tunnel_addresses or not connected else 0.5
                    )
            else:
                tunnel_addresses = cached_tunnel_addresses
            with self.lock:
                if xray_pid != self._statistics_xray_pid:
                    self._statistics_xray_pid = xray_pid
                    self._observed_outbound_inodes = set()
                    self._pending_history_connections = {}
                    self._socket_target_names = {}
                logged_targets = self._associate_xray_targets(outbound)
                target_names = _classify_xray_targets(
                    outbound, logged_targets, tunnel_addresses, connected
                )
                summary = _summarize_xray_connections(
                    connections, inbound_ports, target_names
                )
                new_inodes = (
                    current_inodes - self._observed_outbound_inodes
                    if connected
                    else set()
                )
                for connection in outbound:
                    if connection["inode"] in new_inodes:
                        self._pending_history_connections[connection["inode"]] = (
                            now,
                            connection,
                        )
                ready_history = []
                for inode, (first_seen, connection) in list(
                    self._pending_history_connections.items()
                ):
                    target = target_names.get(inode, {})
                    if not target.get("log_matched") and now - first_seen < 1:
                        continue
                    ready_history.append((connection, target))
                    del self._pending_history_connections[inode]
                self._observed_outbound_inodes = current_inodes
                self._connection_statistics = summary

            if ready_history:
                try:
                    config = normalize_vpn_route_config(read_json(VPN_CONFIG))
                    profile = _vpn_statistics_profile(config)
                except ConfigError:
                    profile = None
                if profile:
                    for connection, target in ready_history:
                        self._target_history.record(
                            profile,
                            connection["remote_address"],
                            connection["remote_port"],
                            target.get("domain"),
                            target.get("route", "unknown"),
                            target.get("outbound_tag"),
                        )

            if time.monotonic() >= next_flush_at:
                try:
                    config = normalize_vpn_route_config(read_json(VPN_CONFIG))
                    retention_days = config["statistics_retention_days"]
                    profile = _vpn_statistics_profile(config)
                except ConfigError:
                    retention_days = 30
                    profile = None
                self._target_history.flush()
                self._target_history.cleanup(profile, retention_days)
                next_flush_at = time.monotonic() + 10
            self._statistics_wakeup.wait(0.25)
            self._statistics_wakeup.clear()

    def target_history(self) -> dict[str, Any]:
        try:
            config = normalize_vpn_route_config(read_json(VPN_CONFIG))
            retention_days = config["statistics_retention_days"]
            profile = _vpn_statistics_profile(config)
        except ConfigError:
            retention_days = 30
            profile = None
        return self._target_history.summary(profile, retention_days)

    def flush_statistics(self) -> None:
        self._target_history.flush()

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
            access_log = _xray_access_log_path(effective)
            try:
                access_position = access_log.stat().st_size if access_log else 0
            except OSError:
                access_position = 0
            atomic_write_json(RUNTIME_CONFIG, effective)
            self.validate_xray(effective)
            self._pending_xray_targets.clear()
            self._socket_target_names.clear()
            self._pending_history_connections.clear()
            self._tunnel_addresses.clear()
            self._next_tunnel_address_refresh = 0
            process = self._spawn(
                "xray", [XRAY_BINARY, "run", "-config", str(RUNTIME_CONFIG)]
            )
            if access_log:
                threading.Thread(
                    target=self._read_xray_access_file,
                    args=(access_log, process, access_position),
                    daemon=True,
                ).start()

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
                self._vpn_reconnect_attempts_total = 0
                self._last_vpn_retry_at = None
                self._next_vpn_retry_at = None
                self._vpn_reconnect_pending = False
                self._vpn_cancel_event.set()
                self._keepalive_wakeup.set()
            self._statistics_wakeup.set()
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
            services["vpn"]["reconnect_attempts"] = self._vpn_reconnect_attempts
            services["vpn"]["reconnect_attempts_total"] = self._vpn_reconnect_attempts_total
            services["vpn"]["last_retry_at"] = self._last_vpn_retry_at
            services["vpn"]["next_retry_at"] = self._next_vpn_retry_at
            keepalive = {
                "last_at": self._last_keepalive_at,
                "ok": self._last_keepalive_ok,
                "error": self._last_keepalive_error,
            }
            connection_statistics = copy.deepcopy(self._connection_statistics)
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
        traffic = _read_interface_statistics()
        now = time.monotonic()
        rx_bytes = int(traffic["rx_bytes"])
        tx_bytes = int(traffic["tx_bytes"])
        rx_rate = 0.0
        tx_rate = 0.0
        with self.lock:
            previous = self._last_traffic_sample
            if bool(traffic["available"]):
                if previous:
                    elapsed = now - previous[0]
                    if elapsed > 0 and rx_bytes >= previous[1] and tx_bytes >= previous[2]:
                        rx_rate = (rx_bytes - previous[1]) / elapsed
                        tx_rate = (tx_bytes - previous[2]) / elapsed
                self._last_traffic_sample = (now, rx_bytes, tx_bytes)
            else:
                self._last_traffic_sample = None
        traffic["rx_rate"] = round(rx_rate, 2)
        traffic["tx_rate"] = round(tx_rate, 2)
        connected_at: float | None = None
        if vpn_ip:
            try:
                connected_at = VPN_CONNECTED.stat().st_mtime
            except OSError:
                pass
        try:
            default_proxy_password: bool | None = xray_uses_default_password(
                read_json(XRAY_CONFIG)
            )
        except ConfigError:
            default_proxy_password = None
        return {
            "services": services,
            "vpn_connected": bool(vpn_ip and services["vpn"]["running"]),
            "vpn_ip": vpn_ip,
            "vpn_routes": vpn_routes,
            "xray_inbounds": _xray_inbound_overview(),
            "active_dns": active_dns,
            "route_table": ROUTE_TABLE,
            "mark": XRAY_MARK,
            "certificate_candidate": self.certificate_candidate(),
            "keepalive": keepalive,
            "security": {
                "default_proxy_password": default_proxy_password,
            },
            "statistics": {
                "vpn_session": {
                    "connected_at": connected_at,
                    "route_count": len(vpn_routes),
                    "dns_count": len(active_dns),
                },
                "traffic": traffic,
                "connections": connection_statistics,
                "sampled_at": time.time(),
            },
        }


manager = ProcessManager()
