#!/bin/sh
set -eu

TABLE="${XRAY_VPN_ROUTE_TABLE:-200}"
MARK="${XRAY_VPN_MARK:-255}"
PRIORITY="${XRAY_VPN_RULE_PRIORITY:-100}"
DNS_PRIORITY="${XRAY_VPN_DNS_RULE_PRIORITY:-99}"
DNS_ROUTES_FILE=/run/proxy2openconnect/dns-routes

# 仅移除应用规则，使带标记套接字回落主路由表。
if [ -f "$DNS_ROUTES_FILE" ]; then
  while IFS=' ' read -r family network; do
    [ -n "$network" ] || continue
    case "$family" in
      4) while ip -4 rule del priority "$DNS_PRIORITY" to "$network" table "$TABLE" 2>/dev/null; do :; done ;;
      6) while ip -6 rule del priority "$DNS_PRIORITY" to "$network" table "$TABLE" 2>/dev/null; do :; done ;;
    esac
  done < "$DNS_ROUTES_FILE"
fi
python3 /opt/proxy2openconnect/app/dns.py restore || true
while ip rule del priority "$PRIORITY" fwmark "$MARK" table "$TABLE" 2>/dev/null; do :; done
while ip -6 rule del priority "$PRIORITY" fwmark "$MARK" table "$TABLE" 2>/dev/null; do :; done
ip route flush table "$TABLE" 2>/dev/null || true
ip -6 route flush table "$TABLE" 2>/dev/null || true
rm -f /run/proxy2openconnect/vpn.connected /run/proxy2openconnect/split-routes
