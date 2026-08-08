import copy
import unittest

from app.core import ConfigError, build_openconnect_command, effective_xray_config, extract_certificate_candidate, validate_server


class EffectiveXrayConfigTests(unittest.TestCase):
    def test_marks_every_network_outbound_without_mutating_source(self):
        source = {
            "inbounds": [{"protocol": "socks", "port": 1080}],
            "outbounds": [
                {"protocol": "freedom", "tag": "direct"},
                {"protocol": "socks", "tag": "next-hop", "streamSettings": {"sockopt": {"tcpFastOpen": True}}},
                {"protocol": "blackhole", "tag": "blocked"},
            ],
        }
        original = copy.deepcopy(source)

        result = effective_xray_config(source, mark=255)

        self.assertEqual(result["outbounds"][0]["streamSettings"]["sockopt"]["mark"], 255)
        self.assertEqual(result["outbounds"][1]["streamSettings"]["sockopt"]["mark"], 255)
        self.assertTrue(result["outbounds"][1]["streamSettings"]["sockopt"]["tcpFastOpen"])
        self.assertNotIn("streamSettings", result["outbounds"][2])
        self.assertEqual(source, original)

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
