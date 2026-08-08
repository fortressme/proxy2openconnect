import unittest

from scripts.split_routes import collect_split_routes


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
        routes, warnings = collect_split_routes({"INTERNAL_IP4_ADDRESS": "10.10.235.227"})

        self.assertEqual(routes, [])
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
