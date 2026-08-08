#!/bin/sh
set -eu

TABLE="${XRAY_VPN_ROUTE_TABLE:-200}"
MARK="${XRAY_VPN_MARK:-255}"
PRIORITY="${XRAY_VPN_RULE_PRIORITY:-100}"
STATE_DIR=/run/xray2cisco
mkdir -p "$STATE_DIR"

ensure_rule() {
  if ! ip rule show | grep -q "fwmark 0x$(printf '%x' "$MARK").*lookup $TABLE"; then
    ip rule add priority "$PRIORITY" fwmark "$MARK" lookup "$TABLE"
  fi
  if ! ip -6 rule show | grep -q "fwmark 0x$(printf '%x' "$MARK").*lookup $TABLE"; then
    ip -6 rule add priority "$PRIORITY" fwmark "$MARK" lookup "$TABLE"
  fi
}

block_xray_egress() {
  ensure_rule
  ip route flush table "$TABLE" 2>/dev/null || true
  ip route add unreachable default table "$TABLE" metric 42760
  ip -6 route flush table "$TABLE" 2>/dev/null || true
  ip -6 route add unreachable default table "$TABLE" metric 42760
  rm -f "$STATE_DIR/vpn.connected"
}

enable_vpn_egress() {
  ensure_rule
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

  ip route flush table "$TABLE" 2>/dev/null || true
  ip route add default dev "$TUNDEV" table "$TABLE"
  ip -6 route flush table "$TABLE" 2>/dev/null || true
  if [ -n "${INTERNAL_IP6_ADDRESS:-}" ]; then
    ip -6 route add default dev "$TUNDEV" table "$TABLE"
  else
    ip -6 route add unreachable default table "$TABLE" metric 42760
  fi
  printf '%s\n' "${INTERNAL_IP4_ADDRESS:-connected}" > "$STATE_DIR/vpn.connected"
}

case "${reason:-}" in
  connect|reconnect)
    enable_vpn_egress
    ;;
  disconnect)
    block_xray_egress
    ;;
  pre-init)
    block_xray_egress
    ;;
  *)
    ;;
esac

exit 0
