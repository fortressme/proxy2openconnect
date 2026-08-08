from __future__ import annotations

import ipaddress
import os
import sys
from collections.abc import Mapping


MAX_ROUTES_PER_GROUP = 4096
ROUTE_GROUPS = (
    (4, "include", "CISCO_SPLIT_INC"),
    (4, "exclude", "CISCO_SPLIT_EXC"),
    (6, "include", "CISCO_IPV6_SPLIT_INC"),
    (6, "exclude", "CISCO_IPV6_SPLIT_EXC"),
)
ROUTE_MODES = frozenset({"all", "vpn", "manual"})


def collect_split_routes(env: Mapping[str, str]) -> tuple[list[tuple[int, str, str]], list[str]]:
    routes: list[tuple[int, str, str]] = []
    warnings: list[str] = []

    for family, action, count_key in ROUTE_GROUPS:
        raw_count = env.get(count_key, "").strip()
        if not raw_count:
            continue
        try:
            count = int(raw_count)
        except ValueError:
            warnings.append(f"忽略无效的 {count_key}={raw_count!r}")
            continue
        if count < 0 or count > MAX_ROUTES_PER_GROUP:
            warnings.append(f"忽略超出范围的 {count_key}={count}")
            continue

        for index in range(count):
            prefix = f"{count_key}_{index}"
            address = env.get(f"{prefix}_ADDR", "").strip()
            mask = env.get(f"{prefix}_MASKLEN", "").strip()
            if family == 4 and not mask:
                mask = env.get(f"{prefix}_MASK", "").strip()
            if not address or not mask:
                warnings.append(f"忽略不完整的 {prefix} 路由")
                continue
            try:
                network = ipaddress.ip_network(f"{address}/{mask}", strict=False)
            except ValueError as exc:
                warnings.append(f"忽略无效的 {prefix} 路由: {exc}")
                continue
            if network.version != family:
                warnings.append(f"忽略地址族不匹配的 {prefix} 路由")
                continue
            routes.append((family, action, network.with_prefixlen))

    return routes, warnings


def _collect_manual_group(value: str, action: str) -> tuple[list[tuple[int, str, str]], list[str]]:
    routes: list[tuple[int, str, str]] = []
    warnings: list[str] = []
    for index, item in enumerate(value.splitlines()):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            network = ipaddress.ip_network(candidate, strict=False)
        except ValueError as exc:
            warnings.append(f"忽略无效的手动{action}路由[{index}]: {exc}")
            continue
        routes.append((network.version, action, network.with_prefixlen))
    return routes, warnings


def collect_route_policy(env: Mapping[str, str]) -> tuple[list[tuple[int, str, str]], list[str]]:
    mode = env.get("XRAY_VPN_ROUTE_MODE", "all").strip().lower()
    if mode not in ROUTE_MODES:
        mode = "all"
        warnings = ["未知路由模式，已回落到 all"]
    else:
        warnings = []

    if mode == "all":
        routes = [(4, "include", "0.0.0.0/0")]
        if env.get("INTERNAL_IP6_ADDRESS", "").strip():
            routes.append((6, "include", "::/0"))
        return routes, warnings
    if mode == "vpn":
        routes, split_warnings = collect_split_routes(env)
        return routes, warnings + split_warnings

    includes, include_warnings = _collect_manual_group(
        env.get("XRAY_VPN_MANUAL_ROUTES", ""), "include"
    )
    excludes, exclude_warnings = _collect_manual_group(
        env.get("XRAY_VPN_MANUAL_EXCLUDE_ROUTES", ""), "exclude"
    )
    return includes + excludes, warnings + include_warnings + exclude_warnings


def main() -> int:
    routes, warnings = collect_route_policy(os.environ)
    for warning in warnings:
        print(f"split-route warning: {warning}", file=sys.stderr)
    for family, action, network in routes:
        print(f"{family} {action} {network}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
