import unittest

from scripts.split_routes import collect_route_policy, collect_split_routes


class SplitRouteTests(unittest.TestCase):
    def test_collects_ipv4_and_ipv6_includes(self):
        routes, warnings = collect_split_routes(
            {
                "CISCO_SPLIT_INC": "2",
                "CISCO_SPLIT_INC_0_ADDR": "10.20.30.99",
                "CISCO_SPLIT_INC_0_MASKLEN": "24",
                "CISCO_SPLIT_INC_1_ADDR": "172.16.0.0",
                "CISCO_SPLIT_INC_1_MASK": "255.255.0.0",
                "CISCO_IPV6_SPLIT_INC": "1",
                "CISCO_IPV6_SPLIT_INC_0_ADDR": "fd00:1234::1",
                "CISCO_IPV6_SPLIT_INC_0_MASKLEN": "48",
            }
        )

        self.assertEqual(warnings, [])
        self.assertEqual(
            routes,
            [
                (4, "include", "10.20.30.0/24"),
                (4, "include", "172.16.0.0/16"),
                (6, "include", "fd00:1234::/48"),
            ],
        )

    def test_collects_excludes_after_includes(self):
        routes, warnings = collect_split_routes(
            {
                "CISCO_SPLIT_INC": "1",
                "CISCO_SPLIT_INC_0_ADDR": "0.0.0.0",
                "CISCO_SPLIT_INC_0_MASKLEN": "0",
                "CISCO_SPLIT_EXC": "1",
                "CISCO_SPLIT_EXC_0_ADDR": "192.0.2.0",
                "CISCO_SPLIT_EXC_0_MASKLEN": "24",
            }
        )

        self.assertEqual(warnings, [])
        self.assertEqual(
            routes,
            [
                (4, "include", "0.0.0.0/0"),
                (4, "exclude", "192.0.2.0/24"),
            ],
        )

    def test_missing_or_invalid_entries_are_ignored(self):
        routes, warnings = collect_split_routes(
            {
                "CISCO_SPLIT_INC": "2",
                "CISCO_SPLIT_INC_0_ADDR": "not-an-ip",
                "CISCO_SPLIT_INC_0_MASKLEN": "24",
                "CISCO_SPLIT_INC_1_ADDR": "10.0.0.0",
            }
        )

        self.assertEqual(routes, [])
        self.assertEqual(len(warnings), 2)

    def test_does_not_invent_a_default_route(self):
        routes, warnings = collect_split_routes({"INTERNAL_IP4_ADDRESS": "192.0.2.10"})

        self.assertEqual(routes, [])
        self.assertEqual(warnings, [])

    def test_all_mode_is_the_default(self):
        routes, warnings = collect_route_policy({})

        self.assertEqual(routes, [(4, "include", "0.0.0.0/0")])
        self.assertEqual(warnings, [])

    def test_all_mode_adds_ipv6_when_available(self):
        routes, _ = collect_route_policy({"XRAY_VPN_ROUTE_MODE": "all", "INTERNAL_IP6_ADDRESS": "fd00::2"})

        self.assertEqual(routes, [(4, "include", "0.0.0.0/0"), (6, "include", "::/0")])

    def test_vpn_mode_uses_downloaded_routes(self):
        routes, warnings = collect_route_policy(
            {
                "XRAY_VPN_ROUTE_MODE": "vpn",
                "CISCO_SPLIT_INC": "1",
                "CISCO_SPLIT_INC_0_ADDR": "10.0.0.0",
                "CISCO_SPLIT_INC_0_MASKLEN": "8",
            }
        )

        self.assertEqual(routes, [(4, "include", "10.0.0.0/8")])
        self.assertEqual(warnings, [])

    def test_vpn_mode_preserves_secured_routes_with_default_exclude(self):
        routes, warnings = collect_route_policy(
            {
                "XRAY_VPN_ROUTE_MODE": "vpn",
                "CISCO_SPLIT_INC": "2",
                "CISCO_SPLIT_INC_0_ADDR": "10.0.0.0",
                "CISCO_SPLIT_INC_0_MASKLEN": "8",
                "CISCO_SPLIT_INC_1_ADDR": "192.0.2.15",
                "CISCO_SPLIT_INC_1_MASKLEN": "32",
                "CISCO_SPLIT_EXC": "1",
                "CISCO_SPLIT_EXC_0_ADDR": "0.0.0.0",
                "CISCO_SPLIT_EXC_0_MASKLEN": "0",
            }
        )

        self.assertEqual(
            routes,
            [
                (4, "include", "10.0.0.0/8"),
                (4, "include", "192.0.2.15/32"),
                (4, "exclude", "0.0.0.0/0"),
            ],
        )
        self.assertEqual(warnings, [])

    def test_manual_mode_uses_configured_routes(self):
        routes, warnings = collect_route_policy(
            {
                "XRAY_VPN_ROUTE_MODE": "manual",
                "XRAY_VPN_MANUAL_ROUTES": "10.0.1.9/24\nfd00:1::1/48",
                "XRAY_VPN_MANUAL_EXCLUDE_ROUTES": "10.0.1.128/25",
            }
        )

        self.assertEqual(
            routes,
            [
                (4, "include", "10.0.1.0/24"),
                (6, "include", "fd00:1::/48"),
                (4, "exclude", "10.0.1.128/25"),
            ],
        )
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
