from __future__ import annotations

import copy
import base64
import json
import os
import re
import shlex
import shutil
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
RUNTIME_CONFIG = Path("/run/xray2cisco/xray-effective.json")
VPN_CONNECTED = Path("/run/xray2cisco/vpn.connected")
XRAY_MARK = int(os.getenv("XRAY_VPN_MARK", "255"))
ROUTE_TABLE = int(os.getenv("XRAY_VPN_ROUTE_TABLE", "200"))
XRAY_BINARY = os.getenv("XRAY_BINARY", "/usr/local/bin/xray")
OPENCONNECT_BINARY = os.getenv("OPENCONNECT_BINARY", "/usr/sbin/openconnect")
VPN_SCRIPT = "/opt/xray2cisco/scripts/vpn-script.sh"


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


def effective_xray_config(config: dict[str, Any], mark: int = XRAY_MARK) -> dict[str, Any]:
    """Return a runtime copy where every network-capable outbound is VPN-marked."""
    result = copy.deepcopy(config)
    outbounds = result.get("outbounds")
    if not isinstance(outbounds, list) or not outbounds:
        raise ConfigError("Xray 配置至少需要一个 outbound")

    for index, outbound in enumerate(outbounds):
        if not isinstance(outbound, dict):
            raise ConfigError(f"outbounds[{index}] 必须是对象")
        if outbound.get("protocol") == "blackhole":
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

    def _append(self, service: ServiceProcess, line: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        service.logs.append(f"{timestamp}  {line.rstrip()}")

    def _read_output(self, service: ServiceProcess, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            with self.lock:
                self._append(service, line)
        code = process.wait()
        with self.lock:
            is_current = service.process is process
            if is_current:
                service.last_exit_code = code
                self._append(service, f"进程已退出，代码 {code}")
                if service.name == "vpn":
                    self.ensure_fail_closed()

    def _spawn(self, name: str, command: list[str], stdin_text: str | None = None) -> None:
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
            config = read_json(VPN_CONFIG)
            secret = password or str(config.get("password", ""))
            if not secret:
                raise ConfigError("请输入 VPN 密码")
            command = build_openconnect_command(config)
            stdin_text = secret + (f"\n{otp}" if otp else "")
            self._spawn("vpn", command, stdin_text)

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
            process = service.process
            if not process or process.poll() is not None:
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
                self.ensure_fail_closed()

    def restart_xray(self) -> None:
        self.stop("xray")
        self.start_xray()

    def ensure_fail_closed(self) -> None:
        script = "/opt/xray2cisco/scripts/route-guard.sh"
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
        vpn_ip = None
        if VPN_CONNECTED.exists():
            vpn_ip = VPN_CONNECTED.read_text(encoding="utf-8").strip()
        return {
            "services": services,
            "vpn_connected": bool(vpn_ip and services["vpn"]["running"]),
            "vpn_ip": vpn_ip,
            "route_table": ROUTE_TABLE,
            "mark": XRAY_MARK,
            "certificate_candidate": self.certificate_candidate(),
        }


manager = ProcessManager()
