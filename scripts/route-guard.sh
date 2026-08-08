#!/bin/sh
set -eu

TABLE="${XRAY_VPN_ROUTE_TABLE:-200}"
MARK="${XRAY_VPN_MARK:-255}"
PRIORITY="${XRAY_VPN_RULE_PRIORITY:-100}"

if ! ip rule show | grep -q "fwmark 0x$(printf '%x' "$MARK").*lookup $TABLE"; then
  ip rule add priority "$PRIORITY" fwmark "$MARK" lookup "$TABLE"
fi
if ! ip -6 rule show | grep -q "fwmark 0x$(printf '%x' "$MARK").*lookup $TABLE"; then
  ip -6 rule add priority "$PRIORITY" fwmark "$MARK" lookup "$TABLE"
fi

ip route flush table "$TABLE" 2>/dev/null || true
ip route add unreachable default table "$TABLE" metric 42760
ip -6 route flush table "$TABLE" 2>/dev/null || true
ip -6 route add unreachable default table "$TABLE" metric 42760
