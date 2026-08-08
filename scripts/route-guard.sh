#!/bin/sh
set -eu

TABLE="${XRAY_VPN_ROUTE_TABLE:-200}"
MARK="${XRAY_VPN_MARK:-255}"
PRIORITY="${XRAY_VPN_RULE_PRIORITY:-100}"

# Remove only this application's policy rules. With no matching rule, marked
# sockets use the normal routing table instead of being blocked.
while ip rule del priority "$PRIORITY" fwmark "$MARK" table "$TABLE" 2>/dev/null; do :; done
while ip -6 rule del priority "$PRIORITY" fwmark "$MARK" table "$TABLE" 2>/dev/null; do :; done
ip route flush table "$TABLE" 2>/dev/null || true
ip -6 route flush table "$TABLE" 2>/dev/null || true
rm -f /run/xray2cisco/vpn.connected /run/xray2cisco/split-routes
