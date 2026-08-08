import copy
import socket
import unittest
from unittest.mock import Mock, patch

from app.core import (
    ConfigError,
    MAX_AUTO_RECONNECT_ATTEMPTS,
    ProcessManager,
    build_openconnect_command,
    effective_xray_config,
    extract_certificate_candidate,
    normalize_vpn_route_config,
    perform_keepalive_request,
    validate_keepalive_url,
    validate_server,
    vpn_route_environment,
)


class ImmediateThread:
    def __init__(self, target, **_):
        self.target = target

    def start(self):
        self.target()


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

    def test_adds_reconnect_and_keepalive_defaults(self):
        config = normalize_vpn_route_config({})

        self.assertTrue(config["auto_reconnect"])
        self.assertEqual(config["auto_reconnect_interval"], 10)
        self.assertFalse(config["keepalive_enabled"])
        self.assertEqual(config["keepalive_interval"], 300)

    def test_validates_enabled_keepalive(self):
        config = normalize_vpn_route_config(
            {
                "keepalive_enabled": True,
                "keepalive_url": "https://intranet.example.test/ping",
                "keepalive_interval": 60,
            }
        )

        self.assertEqual(config["keepalive_url"], "https://intranet.example.test/ping")
        self.assertEqual(config["keepalive_interval"], 60)

    def test_requires_url_when_keepalive_enabled(self):
        with self.assertRaises(ConfigError):
            normalize_vpn_route_config({"keepalive_enabled": True, "keepalive_url": ""})

    def test_rejects_out_of_range_intervals(self):
        with self.assertRaises(ConfigError):
            normalize_vpn_route_config({"auto_reconnect_interval": 0})
        with self.assertRaises(ConfigError):
            normalize_vpn_route_config({"keepalive_interval": 9})


class KeepaliveUrlTests(unittest.TestCase):
    def test_accepts_http_and_https(self):
        self.assertEqual(validate_keepalive_url("http://10.0.0.1/ping"), "http://10.0.0.1/ping")
        self.assertEqual(validate_keepalive_url("https://host.test/health"), "https://host.test/health")

    def test_adds_https_to_address_without_scheme(self):
        self.assertEqual(
            validate_keepalive_url("intranet.example.test/ping"),
            "https://intranet.example.test/ping",
        )

    def test_rejects_credentials_and_unsupported_schemes(self):
        with self.assertRaises(ConfigError):
            validate_keepalive_url("https://user:secret@host.test/ping")
        with self.assertRaises(ConfigError):
            validate_keepalive_url("file:///etc/passwd")

    def test_request_is_marked_and_bound_to_tunnel(self):
        raw_socket = Mock()
        raw_socket.recv.return_value = b"HTTP/1.1 204 No Content\r\n"
        address = ("203.0.113.10", 80)

        with (
            patch("app.core.socket.getaddrinfo", return_value=[(2, 1, 6, "", address)]),
            patch("app.core.socket.socket", return_value=raw_socket),
        ):
            status = perform_keepalive_request("http://intranet.example.test/ping", mark=255)

        self.assertEqual(status, 204)
        raw_socket.setsockopt.assert_any_call(
            socket.SOL_SOCKET,
            getattr(socket, "SO_MARK", 36),
            255,
        )
        raw_socket.setsockopt.assert_any_call(
            socket.SOL_SOCKET,
            getattr(socket, "SO_BINDTODEVICE", 25),
            b"tun0\0",
        )


class AutoReconnectTests(unittest.TestCase):
    def test_stops_after_five_attempts(self):
        self.assertEqual(MAX_AUTO_RECONNECT_ATTEMPTS, 5)

    def test_reconnects_with_cached_password_after_successful_session(self):
        manager = ProcessManager()
        manager._vpn_requested = True
        manager._vpn_password = "secret"
        manager._vpn_ever_connected = True
        manager._vpn_cancel_event = Mock()
        manager._vpn_cancel_event.wait.return_value = False
        config = {"auto_reconnect": True, "auto_reconnect_interval": 10}

        with (
            patch("app.core.read_json", return_value=config),
            patch("app.core.threading.Thread", ImmediateThread),
            patch.object(manager, "_start_vpn_attempt") as start,
        ):
            manager._schedule_vpn_reconnect()

        start.assert_called_once()
        self.assertEqual(manager._vpn_reconnect_attempts, 1)
        self.assertFalse(manager._vpn_reconnect_pending)

    def test_does_not_replay_otp(self):
        manager = ProcessManager()
        manager._vpn_requested = True
        manager._vpn_ever_connected = True
        manager._vpn_requires_otp = True
        config = {"auto_reconnect": True, "auto_reconnect_interval": 10}

        with patch("app.core.read_json", return_value=config):
            manager._schedule_vpn_reconnect()

        self.assertFalse(manager._vpn_reconnect_pending)
        self.assertIn("会话需要新的 OTP", manager.logs("vpn")[-1])


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
