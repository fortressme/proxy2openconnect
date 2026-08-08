from __future__ import annotations

import ipaddress
import os
import re
import sys
from pathlib import Path


STATE_DIR = Path("/run/proxy2openconnect")
RESOLV_CONF = Path("/etc/resolv.conf")
BACKUP_NAME = "resolv.conf.original"
ROUTES_NAME = "dns-routes"
DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)


class DnsConfigError(ValueError):
    pass


def parse_servers(value: str, limit: int = 3) -> list[str]:
    servers: list[str] = []
    for item in re.split(r"[\s,]+", value.strip()):
        if not item:
            continue
        try:
            address = ipaddress.ip_address(item).compressed
        except ValueError as exc:
            raise DnsConfigError(f"无效的 DNS 服务器 IP: {item}") from exc
        if address not in servers:
            servers.append(address)
        if len(servers) >= limit:
            break
    return servers


def parse_search_domains(value: str, limit: int = 6) -> list[str]:
    domains: list[str] = []
    for item in value.split():
        candidate = item.strip().rstrip(".")
        if not candidate:
            continue
        if not DOMAIN_PATTERN.fullmatch(candidate):
            continue
        if candidate not in domains:
            domains.append(candidate)
        if len(domains) >= limit:
            break
    return domains


def restore_dns(
    resolv_conf: Path = RESOLV_CONF,
    state_dir: Path = STATE_DIR,
) -> bool:
    backup = state_dir / BACKUP_NAME
    restored = False
    if backup.exists():
        resolv_conf.write_bytes(backup.read_bytes())
        backup.unlink()
        restored = True
    for name in (ROUTES_NAME, "dns-active"):
        path = state_dir / name
        if path.exists():
            path.unlink()
    return restored


def apply_dns(
    mode: str,
    manual_servers: str = "",
    pushed_servers: str = "",
    search_domains: str = "",
    resolv_conf: Path = RESOLV_CONF,
    state_dir: Path = STATE_DIR,
) -> list[str]:
    if mode == "system":
        restore_dns(resolv_conf, state_dir)
        return []
    if mode not in {"vpn", "manual"}:
        raise DnsConfigError(f"未知的全局 DNS 模式: {mode}")

    source = manual_servers if mode == "manual" else pushed_servers
    servers = parse_servers(source)
    if not servers:
        label = "手动配置" if mode == "manual" else "VPN 网关"
        raise DnsConfigError(f"{label}没有提供可用的 DNS 服务器")

    state_dir.mkdir(parents=True, exist_ok=True)
    backup = state_dir / BACKUP_NAME
    original = resolv_conf.read_bytes() if resolv_conf.exists() else b""
    if not backup.exists():
        backup.write_bytes(original)
        backup.chmod(0o600)

    preserved_options = [
        line.strip()
        for line in original.decode("utf-8", errors="replace").splitlines()
        if line.strip().startswith("options ")
    ]
    domains = parse_search_domains(search_domains)
    lines = ["# Managed by proxy2openconnect while the VPN is connected"]
    lines.extend(f"nameserver {server}" for server in servers)
    if domains:
        lines.append(f"search {' '.join(domains)}")
    lines.extend(preserved_options)
    resolv_conf.write_text("\n".join(lines) + "\n", encoding="utf-8")

    route_lines = []
    for server in servers:
        address = ipaddress.ip_address(server)
        route_lines.append(f"{address.version} {address}/{address.max_prefixlen}")
    (state_dir / ROUTES_NAME).write_text("\n".join(route_lines) + "\n", encoding="utf-8")
    (state_dir / "dns-active").write_text("\n".join(servers) + "\n", encoding="utf-8")
    return servers


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    action = arguments[0] if arguments else ""
    try:
        if action == "restore":
            restore_dns()
            return 0
        if action != "apply":
            raise DnsConfigError("用法: python -m app.dns apply|restore")
        mode = os.getenv("XRAY_VPN_DNS_MODE", "system")
        pushed = " ".join(
            filter(
                None,
                (
                    os.getenv("INTERNAL_IP4_DNS", ""),
                    os.getenv("INTERNAL_IP6_DNS", ""),
                ),
            )
        )
        servers = apply_dns(
            mode=mode,
            manual_servers=os.getenv("XRAY_VPN_DNS_SERVERS", ""),
            pushed_servers=pushed,
            search_domains=os.getenv("CISCO_DEF_DOMAIN", ""),
        )
        if servers:
            print(f"已应用全局 DNS: {', '.join(servers)}")
        return 0
    except (DnsConfigError, OSError) as exc:
        print(f"全局 DNS 配置失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
