#!/bin/sh
set -eu

umask 077
mkdir -p "${DATA_DIR:-/data}/xray" "${DATA_DIR:-/data}/vpn" /run/xray2cisco

if [ ! -f "${DATA_DIR:-/data}/xray/config.json" ]; then
  cp /opt/xray2cisco/defaults/xray-config.json "${DATA_DIR:-/data}/xray/config.json"
fi

if [ ! -f "${DATA_DIR:-/data}/vpn/config.json" ]; then
  cp /opt/xray2cisco/defaults/vpn-config.json "${DATA_DIR:-/data}/vpn/config.json"
fi

chmod 600 "${DATA_DIR:-/data}/xray/config.json" "${DATA_DIR:-/data}/vpn/config.json"
/opt/xray2cisco/scripts/route-guard.sh

exec "$@"

