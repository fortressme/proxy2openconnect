import copy
import unittest

from app.core import (
    ConfigError,
    build_openconnect_command,
    effective_xray_config,
    extract_certificate_candidate,
    normalize_vpn_route_config,
    validate_server,
    vpn_route_environment,
)


class EffectiveXrayConfigTests(unittest.TestCase):
    def test_marks_only_selected_outbounds_without_mutating_source(self):
        source = {
            "inbounds": [{"protocol": "socks", "port": 1080}],
            "outbounds": [
                {"protocol": "freedom", "tag": "direct"},
                {"protocol": "freedom", "tag": "vpn-out", "streamSettings": {"sockopt": {"tcpFastOpen": True}}},
                {"protocol": "blackhole", "tag": "blocked"},
            ],
        }
        original = copy.deepcopy(source)

        result = effective_xray_config(source, mark=255, outbound_tags={"vpn-out"})

        self.assertNotIn("streamSettings", result["outbounds"][0])
        self.assertEqual(result["outbounds"][1]["streamSettings"]["sockopt"]["mark"], 255)
        self.assertTrue(result["outbounds"][1]["streamSettings"]["sockopt"]["tcpFastOpen"])
        self.assertNotIn("streamSettings", result["outbounds"][2])
        self.assertEqual(source, original)

    def test_does_not_mark_unselected_existing_sockopt(self):
        source = {
            "inbounds": [{"protocol": "socks", "port": 1080}],
            "outbounds": [
                {"protocol": "socks", "tag": "next-hop", "streamSettings": {"sockopt": {"mark": 42}}},
                {"protocol": "freedom", "tag": "vpn-out"},
            ],
        }

        result = effective_xray_config(source, mark=255, outbound_tags={"vpn-out"})

        self.assertEqual(result["outbounds"][0]["streamSettings"]["sockopt"]["mark"], 42)
        self.assertEqual(result["outbounds"][1]["streamSettings"]["sockopt"]["mark"], 255)

    def test_default_selection_marks_vpn_out(self):
        source = {
            "inbounds": [{"protocol": "socks", "port": 1080}],
            "outbounds": [
                {"protocol": "freedom", "tag": "direct"},
                {"protocol": "freedom", "tag": "vpn-out"},
            ],
        }

        result = effective_xray_config(source, mark=255)

        self.assertNotIn("streamSettings", result["outbounds"][0])
        self.assertEqual(result["outbounds"][1]["streamSettings"]["sockopt"]["mark"], 255)

    def test_rejects_missing_outbound(self):
        with self.assertRaises(ConfigError):
            effective_xray_config({"inbounds": [{}]})


class OpenConnectCommandTests(unittest.TestCase):
    def base_config(self):
        return {
            "server": "vpn.company.test",
            "username": "alice",
            "authgroup": "employees",
            "servercert": "sha256:abc123",
            "useragent": "AnyConnect Windows 4.10.08029",
            "certificate": "",
            "sslkey": "",
            "cafile": "",
            "no_dtls": True,
            "disable_ipv6": True,
            "reconnect_timeout": 120,
            "extra_args": ["--os=win", "--version-string=4.10.08029"],
        }

    def test_builds_safe_argv(self):
        command = build_openconnect_command(self.base_config())
        self.assertIn("--protocol=anyconnect", command)
        self.assertIn("--interface=tun0", command)
        self.assertIn("--passwd-on-stdin", command)
        self.assertIn("--authgroup=employees", command)
        self.assertIn("--useragent=AnyConnect Windows 4.10.08029", command)
        self.assertIn("--os=win", command)
        self.assertIn("--version-string=4.10.08029", command)
        self.assertIn("--no-dtls", command)
        self.assertEqual(command[-1], "https://vpn.company.test")

    def test_rejects_controlled_extra_option(self):
        config = self.base_config()
        config["extra_args"] = ["--script=/tmp/unsafe"]
        with self.assertRaises(ConfigError):
            build_openconnect_command(config)

    def test_rejects_credentials_in_url(self):
        with self.assertRaises(ConfigError):
            validate_server("https://alice:secret@vpn.company.test")

    def test_limits_certificate_paths_to_data_volume(self):
        config = self.base_config()
        config["certificate"] = "/etc/shadow"
        with self.assertRaises(ConfigError):
            build_openconnect_command(config)


class VpnRouteConfigTests(unittest.TestCase):
    def test_defaults_to_all_mode(self):
        config = normalize_vpn_route_config({})

        self.assertEqual(config["route_mode"], "all")
        self.assertEqual(config["manual_routes"], [])

    def test_normalizes_manual_networks_and_environment(self):
        config = normalize_vpn_route_config(
            {
                "route_mode": "manual",
                "manual_routes": ["10.20.30.99/24", "fd00::1/48", "10.20.30.0/24"],
                "manual_exclude_routes": ["10.20.30.128/25"],
            }
        )

        self.assertEqual(config["manual_routes"], ["10.20.30.0/24", "fd00::/48"])
        self.assertEqual(config["manual_exclude_routes"], ["10.20.30.128/25"])
        self.assertEqual(vpn_route_environment(config)["XRAY_VPN_ROUTE_MODE"], "manual")

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ConfigError):
            normalize_vpn_route_config({"route_mode": "unknown"})

    def test_requires_manual_include(self):
        with self.assertRaises(ConfigError):
            normalize_vpn_route_config({"route_mode": "manual", "manual_routes": []})

    def test_rejects_invalid_manual_network(self):
        with self.assertRaises(ConfigError):
            normalize_vpn_route_config({"route_mode": "manual", "manual_routes": ["invalid"]})


class CertificateCandidateTests(unittest.TestCase):
    def test_extracts_host_and_valid_sha256_pin(self):
        lines = [
            'Certificate from VPN server "vpn.example.test" failed verification.',
            '    --servercert pin-sha256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
        ]
        self.assertEqual(
            extract_certificate_candidate(lines),
            {"host": "vpn.example.test", "pin": "pin-sha256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
        )

    def test_rejects_malformed_pin(self):
        lines = [
            'Certificate from VPN server "vpn.example.test" failed verification.',
            '    --servercert pin-sha256:not-a-real-pin',
        ]
        self.assertIsNone(extract_certificate_candidate(lines))


if __name__ == "__main__":
    unittest.main()
