import subprocess
import unittest
from unittest.mock import patch

import fleet_file_deploy as deployment
import fleet_mesh_discovery as discovery
from fleet_archive import ArchiveError


class DiscoveryTests(unittest.TestCase):
    def test_dns_prefers_ipv4_and_normalizes_duplicate_case(self):
        records = discovery.parse_shared_dns(
            "fd00::1 AP-one\n192.0.2.1 AP-one\n192.0.2.1 ap-one\n"
            "192.0.2.2 desktop\n# comment\n"
        )
        self.assertEqual(records, ({"AP-one": "192.0.2.1", "desktop": "192.0.2.2"}, {}))

    def test_invalid_empty_ambiguous_and_excess_records_fail(self):
        for data in ("", "bad node\n", "192.0.2.1\n", "192.0.2.1 bad/name\n"):
            with self.subTest(data=data), self.assertRaises(ArchiveError):
                discovery.parse_shared_dns(data)
        data = "".join(f"192.0.2.1 node-{i}\n" for i in range(discovery.MAX_CANDIDATES + 1))
        with self.assertRaisesRegex(ArchiveError, "Too many"):
            discovery.parse_shared_dns(data)

    def test_ambiguous_candidate_does_not_hide_other_nodes(self):
        valid, ambiguous = discovery.parse_shared_dns(
            "192.0.2.1 router\n192.0.2.2 client\n192.0.2.3 client\n"
        )
        self.assertEqual(valid, {"router": "192.0.2.1"})
        self.assertEqual(ambiguous, {"client": "192.0.2.2, 192.0.2.3"})

    def test_seed_discovery_excludes_clients_and_wrong_hostname(self):
        dns = ("192.0.2.1 seed\n192.0.2.2 router\n"
               "192.0.2.3 desktop\n192.0.2.4 wrong\n")

        def remote(node, command, timeout):
            if command == discovery.DNS_COMMAND:
                return subprocess.CompletedProcess([], 0, dns, "")
            self.assertEqual(command, discovery.MESH_COMMAND)
            return subprocess.CompletedProcess([], 1 if node.name == "desktop" else 0, "", "")

        def inspect(nodes):
            return [
                {"name": node.name, "host": node.host, "status": "available",
                 "hostname": ("seed" if node.name == "mesh-seed" else
                              "another" if node.name == "wrong" else node.name),
                 "board": "aruba,ap-303", "model": "AP", "model_id": "aruba_ap-303", "error": None}
                for node in nodes
            ]

        with patch.object(deployment.fleet, "remote", side_effect=remote):
            nodes, details = discovery.discover(deployment.fleet, [], "root@192.0.2.1", inspect)
        self.assertEqual([node.name for node in nodes], ["router", "seed"])
        self.assertEqual([node.mesh_ip for node in nodes], ["192.0.2.2", "192.0.2.1"])
        self.assertEqual([item["name"] for item in details if item["status"] != "available"],
                         ["desktop", "wrong"])
        self.assertTrue(all(item["source"] == "discovered" for item in details))

    def test_inventory_deduplicates_names_and_addresses_and_fallback_seed(self):
        inventory = [
            deployment.fleet.Node({"name": "custom", "host": "root@192.0.2.1", "mesh_ip": "192.0.2.1"}),
            deployment.fleet.Node({"name": "other", "host": "root@192.0.2.3", "mesh_ip": "192.0.2.3"}),
        ]
        dns = "192.0.2.1 seed\n192.0.2.2 new\n192.0.2.3 other\n"

        def remote(node, command, timeout):
            if command == discovery.DNS_COMMAND and node.name == "custom":
                return subprocess.CompletedProcess([], 1, "", "offline")
            return subprocess.CompletedProcess([], 0, dns if command == discovery.DNS_COMMAND else "", "")

        def inspect(nodes):
            return [
                {"name": node.name, "host": node.host, "status": "available",
                 "hostname": "seed" if node.name == "custom" else node.name,
                 "board": "vendor,board", "model": "Router", "model_id": "vendor_board", "error": None}
                for node in nodes
            ]

        with patch.object(deployment.fleet, "remote", side_effect=remote):
            nodes, details = discovery.discover(deployment.fleet, inventory, "", inspect)
        self.assertEqual([node.name for node in nodes], ["custom", "other", "new"])
        self.assertEqual([item["source"] for item in details], ["inventory", "inventory", "discovered"])

    def test_discovery_errors_are_not_silently_ignored(self):
        with self.assertRaisesRegex(ArchiveError, "Enter a mesh seed"):
            discovery.discover(deployment.fleet, [], "", lambda nodes: [])
        node = deployment.fleet.Node({"name": "seed", "host": "root@192.0.2.1"})
        with patch.object(deployment.fleet, "remote",
                          return_value=subprocess.CompletedProcess([], 255, "", "host key rejected")):
            with self.assertRaisesRegex(ArchiveError, "host key rejected"):
                discovery.discover(deployment.fleet, [node], "", lambda nodes: [])


if __name__ == "__main__":
    unittest.main()
