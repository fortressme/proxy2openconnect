import tempfile
import unittest
from pathlib import Path

from app.dns import DnsConfigError, apply_dns, parse_search_domains, parse_servers, restore_dns


class DnsConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state = self.root / "state"
        self.resolv = self.root / "resolv.conf"
        self.original = "nameserver 127.0.0.11\noptions ndots:0\n"
        self.resolv.write_text(self.original, encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_applies_manual_dns_and_restores_original(self):
        servers = apply_dns(
            mode="manual",
            manual_servers="10.0.0.53\n2001:db8::53",
            resolv_conf=self.resolv,
            state_dir=self.state,
        )

        self.assertEqual(servers, ["10.0.0.53", "2001:db8::53"])
        contents = self.resolv.read_text(encoding="utf-8")
        self.assertIn("nameserver 10.0.0.53", contents)
        self.assertIn("nameserver 2001:db8::53", contents)
        self.assertIn("options ndots:0", contents)
        self.assertEqual(
            (self.state / "dns-routes").read_text(encoding="utf-8"),
            "4 10.0.0.53/32\n6 2001:db8::53/128\n",
        )

        self.assertTrue(restore_dns(self.resolv, self.state))
        self.assertEqual(self.resolv.read_text(encoding="utf-8"), self.original)
        self.assertFalse((self.state / "dns-routes").exists())

    def test_uses_vpn_pushed_dns_and_search_domain(self):
        apply_dns(
            mode="vpn",
            pushed_servers="10.20.0.53 10.20.0.54",
            search_domains="corp.example.test",
            resolv_conf=self.resolv,
            state_dir=self.state,
        )

        contents = self.resolv.read_text(encoding="utf-8")
        self.assertIn("nameserver 10.20.0.53", contents)
        self.assertIn("search corp.example.test", contents)

    def test_system_mode_restores_previous_override(self):
        apply_dns(
            mode="manual",
            manual_servers="10.0.0.53",
            resolv_conf=self.resolv,
            state_dir=self.state,
        )

        self.assertEqual(apply_dns("system", resolv_conf=self.resolv, state_dir=self.state), [])
        self.assertEqual(self.resolv.read_text(encoding="utf-8"), self.original)

    def test_rejects_invalid_or_missing_servers(self):
        with self.assertRaises(DnsConfigError):
            parse_servers("not-an-ip")
        with self.assertRaises(DnsConfigError):
            apply_dns("manual", resolv_conf=self.resolv, state_dir=self.state)

    def test_filters_invalid_search_domains(self):
        self.assertEqual(
            parse_search_domains("corp.example.test invalid_domain good.test"),
            ["corp.example.test", "good.test"],
        )


if __name__ == "__main__":
    unittest.main()
