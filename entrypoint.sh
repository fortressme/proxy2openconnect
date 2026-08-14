#!/bin/sh
set -eu

umask 077
mkdir -p "${DATA_DIR:-/data}/xray" "${DATA_DIR:-/data}/vpn" /run/proxy2openconnect

if [ ! -f "${DATA_DIR:-/data}/xray/config.json" ]; then
  cp /opt/proxy2openconnect/defaults/xray-config.json "${DATA_DIR:-/data}/xray/config.json"
fi

if [ ! -f "${DATA_DIR:-/data}/xray/link.json" ]; then
  cp /opt/proxy2openconnect/defaults/xray-link-config.json "${DATA_DIR:-/data}/xray/link.json"
fi

if [ ! -f "${DATA_DIR:-/data}/vpn/config.json" ]; then
  cp /opt/proxy2openconnect/defaults/vpn-config.json "${DATA_DIR:-/data}/vpn/config.json"
fi

chmod 600 "${DATA_DIR:-/data}/xray/config.json" "${DATA_DIR:-/data}/xray/link.json" "${DATA_DIR:-/data}/vpn/config.json"
/opt/proxy2openconnect/scripts/route-guard.sh

exec "$@"
