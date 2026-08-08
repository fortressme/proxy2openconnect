#!/bin/sh
set -eu

TABLE="${XRAY_VPN_ROUTE_TABLE:-200}"
MARK="${XRAY_VPN_MARK:-255}"
PRIORITY="${XRAY_VPN_RULE_PRIORITY:-100}"
DNS_PRIORITY="${XRAY_VPN_DNS_RULE_PRIORITY:-99}"
STATE_DIR=/run/proxy2openconnect
ROUTES_FILE="$STATE_DIR/split-routes"
DNS_ROUTES_FILE="$STATE_DIR/dns-routes"
mkdir -p "$STATE_DIR"

ensure_ipv4_rule() {
  if ! ip rule show | grep -q "fwmark 0x$(printf '%x' "$MARK").*lookup $TABLE"; then
    ip rule add priority "$PRIORITY" fwmark "$MARK" lookup "$TABLE"
  fi
}

ensure_ipv6_rule() {
  if ! ip -6 rule show | grep -q "fwmark 0x$(printf '%x' "$MARK").*lookup $TABLE"; then
    ip -6 rule add priority "$PRIORITY" fwmark "$MARK" lookup "$TABLE"
  fi
}

remove_policy_rules() {
  while ip rule del priority "$PRIORITY" fwmark "$MARK" table "$TABLE" 2>/dev/null; do :; done
  while ip -6 rule del priority "$PRIORITY" fwmark "$MARK" table "$TABLE" 2>/dev/null; do :; done
}

remove_dns_rules() {
  [ -f "$DNS_ROUTES_FILE" ] || return 0
  while IFS=' ' read -r family network; do
    [ -n "$network" ] || continue
    case "$family" in
      4) while ip -4 rule del priority "$DNS_PRIORITY" to "$network" table "$TABLE" 2>/dev/null; do :; done ;;
      6) while ip -6 rule del priority "$DNS_PRIORITY" to "$network" table "$TABLE" 2>/dev/null; do :; done ;;
    esac
  done < "$DNS_ROUTES_FILE"
}

flush_policy_routes() {
  ip route flush table "$TABLE" 2>/dev/null || true
  ip -6 route flush table "$TABLE" 2>/dev/null || true
}

enable_direct_fallback() {
  remove_dns_rules
  python3 /opt/proxy2openconnect/app/dns.py restore || true
  remove_policy_rules
  flush_policy_routes
  rm -f "$ROUTES_FILE"
  rm -f "$STATE_DIR/vpn.connected"
}

install_dns_routes() {
  [ -f "$DNS_ROUTES_FILE" ] || return 0
  while IFS=' ' read -r family network; do
    [ -n "$network" ] || continue
    case "$family" in
      4)
        ip -4 route replace "$network" dev "$TUNDEV" table "$TABLE"
        ip -4 rule add priority "$DNS_PRIORITY" to "$network" lookup "$TABLE"
        ;;
      6)
        ip -6 route replace "$network" dev "$TUNDEV" table "$TABLE"
        ip -6 rule add priority "$DNS_PRIORITY" to "$network" lookup "$TABLE"
        ;;
    esac
  done < "$DNS_ROUTES_FILE"
}

configure_global_dns() {
  if python3 /opt/proxy2openconnect/app/dns.py apply; then
    if ! install_dns_routes; then
      echo "全局 DNS 专用路由安装失败，已恢复容器默认 DNS" >&2
      remove_dns_rules
      python3 /opt/proxy2openconnect/app/dns.py restore || true
    fi
  else
    python3 /opt/proxy2openconnect/app/dns.py restore || true
  fi
}

install_split_routes() {
  python3 /opt/proxy2openconnect/scripts/split_routes.py > "$ROUTES_FILE"
  while IFS=' ' read -r family action network; do
    [ -n "$network" ] || continue
    case "$family:$action" in
      4:include)
        ip -4 route replace "$network" dev "$TUNDEV" table "$TABLE"
        ;;
      4:exclude)
        ip -4 route replace throw "$network" table "$TABLE"
        ;;
      6:include)
        ip -6 route replace "$network" dev "$TUNDEV" table "$TABLE"
        ;;
      6:exclude)
        ip -6 route replace throw "$network" table "$TABLE"
        ;;
    esac
  done < "$ROUTES_FILE"

  if grep -q '^4 include ' "$ROUTES_FILE"; then
    ensure_ipv4_rule
  fi
  if grep -q '^6 include ' "$ROUTES_FILE"; then
    ensure_ipv6_rule
  fi
}

enable_vpn_egress() {
  if [ -n "${INTERNAL_IP4_MTU:-}" ]; then
    ip link set dev "$TUNDEV" mtu "$INTERNAL_IP4_MTU"
  fi
  if [ -n "${INTERNAL_IP4_ADDRESS:-}" ]; then
    prefix="${INTERNAL_IP4_NETMASKLEN:-}"
    if [ -z "$prefix" ] && [ -n "${INTERNAL_IP4_NETMASK:-}" ]; then
      prefix="$(python3 -c 'import ipaddress, os; print(ipaddress.IPv4Network("0.0.0.0/" + os.environ["INTERNAL_IP4_NETMASK"]).prefixlen)')"
    fi
    ip -4 addr replace "${INTERNAL_IP4_ADDRESS}/${prefix:-32}" dev "$TUNDEV"
  fi
  if [ -n "${INTERNAL_IP6_ADDRESS:-}" ]; then
    ip -6 addr replace "${INTERNAL_IP6_ADDRESS}/${INTERNAL_IP6_NETMASK:-128}" dev "$TUNDEV"
  fi
  ip link set dev "$TUNDEV" up

  remove_dns_rules
  remove_policy_rules
  flush_policy_routes
  install_split_routes
  configure_global_dns
  printf '%s\n' "${INTERNAL_IP4_ADDRESS:-connected}" > "$STATE_DIR/vpn.connected"
}

case "${reason:-}" in
  connect|reconnect)
    enable_vpn_egress
    ;;
  disconnect)
    enable_direct_fallback
    ;;
  pre-init)
    enable_direct_fallback
    ;;
  *)
    ;;
esac

exit 0
