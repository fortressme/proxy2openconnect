from __future__ import annotations

import copy
import os
import tempfile
import unittest


_DATA_DIR = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _DATA_DIR.name
os.environ["XRAY_BINARY"] = os.path.join(_DATA_DIR.name, "missing-xray")

from app.core import (  # noqa: E402
    ConfigError,
    XRAY_OFFLINE_BLOCK_TAG,
    effective_xray_config,
    normalize_xray_link_config,
)


class XrayLinkConfigTests(unittest.TestCase):
    def test_normalizes_urls_wildcards_and_duplicates(self) -> None:
        result = normalize_xray_link_config(
            {
                "mode": "block_sites",
                "blocked_sites": [
                    "https://Example.com/health",
                    "example.com",
                    "*.Example.org",
                ],
            }
        )

        self.assertEqual(result["mode"], "block_sites")
        self.assertEqual(result["blocked_sites"], ["example.com", "*.example.org"])

    def test_block_mode_requires_at_least_one_site(self) -> None:
        with self.assertRaisesRegex(ConfigError, "至少需要填写一个域名"):
            normalize_xray_link_config({"mode": "block_sites", "blocked_sites": []})

    def test_rejects_ip_addresses(self) -> None:
        with self.assertRaisesRegex(ConfigError, "不能使用 IP 地址"):
            normalize_xray_link_config(
                {"mode": "block_sites", "blocked_sites": ["https://192.0.2.1/check"]}
            )


class EffectiveXrayConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "inbounds": [{"protocol": "socks", "port": 1080}],
            "outbounds": [{"protocol": "freedom", "tag": "vpn-out"}],
            "routing": {
                "rules": [
                    {"type": "field", "domain": ["example.net"], "outboundTag": "vpn-out"}
                ]
            },
        }

    def test_injects_offline_block_rule_before_user_rules(self) -> None:
        original = copy.deepcopy(self.config)
        result = effective_xray_config(
            self.config,
            mark=255,
            outbound_tags={"vpn-out"},
            blocked_sites=["health.example.com", "*.check.example.org"],
        )

        self.assertEqual(self.config, original)
        self.assertEqual(result["outbounds"][0]["streamSettings"]["sockopt"]["mark"], 255)
        self.assertEqual(result["outbounds"][-1]["protocol"], "blackhole")
        self.assertEqual(result["outbounds"][-1]["settings"]["response"]["type"], "none")
        self.assertEqual(result["outbounds"][-1]["tag"], XRAY_OFFLINE_BLOCK_TAG)
        self.assertEqual(
            result["routing"]["rules"][0],
            {
                "type": "field",
                "domain": ["full:health.example.com", "domain:check.example.org"],
                "outboundTag": XRAY_OFFLINE_BLOCK_TAG,
            },
        )
        self.assertEqual(result["routing"]["rules"][1], original["routing"]["rules"][0])

    def test_reserved_outbound_tag_is_rejected(self) -> None:
        self.config["outbounds"].append(
            {"protocol": "blackhole", "tag": XRAY_OFFLINE_BLOCK_TAG}
        )
        with self.assertRaisesRegex(ConfigError, "由 VPN 联动功能保留"):
            effective_xray_config(self.config, blocked_sites=["example.com"])


if __name__ == "__main__":
    unittest.main()
