import subprocess
import unittest
from unittest.mock import patch

import fleet_file_deploy as deployment
from fleet_archive import ArchiveError, Change, Entry


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.node = deployment.fleet.Node({
            "name": "a", "host": "root@192.0.2.1", "mesh_ip": "192.0.2.1",
        })

    def test_remote_file_read_is_bounded_and_decoded(self):
        output = "FILE -rwxr-xr-x 4\n" + b"data".hex() + "\n"
        with patch.object(deployment.fleet, "remote",
                          return_value=subprocess.CompletedProcess([], 0, output, "")) as remote:
            self.assertEqual(deployment.read_node_file(self.node, "usr/sbin/tool"),
                             Entry("file", b"data", 0o755))
        command = remote.call_args.args[1]
        self.assertIn("wc -c", command)
        self.assertIn("symlink in path", command)

    def test_remote_file_read_preserves_special_mode_bits(self):
        output = "FILE -rwsr-Sr-t 1\n78\n"
        with patch.object(deployment.fleet, "remote",
                          return_value=subprocess.CompletedProcess([], 0, output, "")):
            self.assertEqual(deployment.read_node_file(self.node, "opt/tool"),
                             Entry("file", b"x", 0o7745))

    def test_preflight_merges_without_uploading_or_applying(self):
        change = Change("etc/config/test", Entry("file", b"a\n\nx\n", 0o600),
                        Entry("file", b"a\n\ny\n", 0o600))
        before = Entry("file", b"a\n\nx\n", 0o600)
        pending = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(deployment.fleet, "remote", return_value=pending) as remote, \
             patch.object(deployment, "read_node_file", return_value=before), \
             patch.object(deployment.fleet, "push") as push:
            plans = deployment.preflight([self.node], [change])
        self.assertEqual(plans[0].files[0].after.data, b"a\n\ny\n")
        remote.assert_called_once()
        push.assert_not_called()

    def test_protected_recovery_helper_rejected(self):
        change = Change("usr/sbin/picot-files-sync", None, Entry("file", b"x", 0o755))
        with self.assertRaises(ArchiveError):
            deployment.preflight([self.node], [change])

    def test_duplicate_mesh_addresses_rejected(self):
        other = deployment.fleet.Node({
            "name": "b", "host": "root@192.0.2.2", "mesh_ip": "192.0.2.1",
        })
        change = Change("opt/test", None, Entry("file", b"x", 0o600))
        with self.assertRaises(ArchiveError):
            deployment.preflight([self.node, other], [change])

    def test_manifest_contains_hashes_not_file_contents(self):
        before = Entry("file", b"old-content", 0o600)
        after = Entry("file", b"new-content", 0o755)
        change = Change("usr/sbin/tool", before, after)
        plan = deployment.NodePlan(self.node, (deployment.PlannedFile(change, before, after),))
        manifest = deployment._manifest(plan)
        self.assertNotIn(before.data, manifest)
        self.assertNotIn(after.data, manifest)
        self.assertIn(b"modify|usr/sbin/tool|", manifest)

    def test_apk_package_check_simulates_offline(self):
        outputs = [subprocess.CompletedProcess([], 0, "apk\n", ""),
                   subprocess.CompletedProcess([], 0, "OK", "")]
        with patch.object(deployment.fleet, "remote", side_effect=outputs) as remote:
            self.assertEqual(deployment.check_packages(self.node, ("nano",)), "apk")
        self.assertIn("apk --simulate add -- nano", remote.call_args.args[1])

    def test_opkg_package_check_uses_metadata_not_broken_simulation(self):
        outputs = [subprocess.CompletedProcess([], 0, "opkg\n", ""),
                   subprocess.CompletedProcess([], 0, "", "")]
        with patch.object(deployment.fleet, "remote", side_effect=outputs) as remote:
            self.assertEqual(deployment.check_packages(self.node, ("nano",)), "opkg")
        self.assertIn("opkg info", remote.call_args.args[1])

    def test_missing_package_is_rejected(self):
        outputs = [subprocess.CompletedProcess([], 0, "apk\n", ""),
                   subprocess.CompletedProcess([], 1, "", "package not found")]
        with patch.object(deployment.fleet, "remote", side_effect=outputs):
            with self.assertRaisesRegex(ArchiveError, "package not found"):
                deployment.check_packages(self.node, ("missing",))

    def test_package_only_preflight_and_invalid_name(self):
        pending = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(deployment.fleet, "remote", return_value=pending), \
             patch.object(deployment, "check_packages", return_value="apk"):
            plans = deployment.preflight([self.node], [], ("nano",))
            self.assertEqual(plans[0].packages, ("nano",))
            with self.assertRaises(ArchiveError):
                deployment.preflight([self.node], [], ("nano;reboot",))

    def test_partial_package_failure_stops_and_reports(self):
        second = deployment.fleet.Node({
            "name": "b", "host": "root@192.0.2.2", "mesh_ip": "192.0.2.2",
        })
        plans = [deployment.NodePlan(node, (), ("nano",), "apk")
                 for node in (self.node, second)]
        with patch.object(deployment, "check_packages", return_value="apk"), \
             patch.object(deployment, "_install"), \
             patch.object(deployment, "_health", return_value=True), \
             patch.object(deployment, "install_packages",
                          side_effect=[None, ArchiveError("failed")]) as install, \
             patch.object(deployment, "_stage") as stage:
            with self.assertRaises(ArchiveError) as error:
                deployment.deploy(plans, "none")
        self.assertEqual(install.call_count, 2)
        stage.assert_not_called()
        self.assertIn("attempted on: a, b", str(error.exception))
        self.assertIn("completed on: a", str(error.exception))
        self.assertIn("not rolled back", str(error.exception))

    def test_packages_are_installed_before_file_staging(self):
        change = Change("opt/tool", None, Entry("file", b"x", 0o600))
        plan = deployment.NodePlan(
            self.node, (deployment.PlannedFile(change, None, change.edited),), ("nano",), "opkg")
        order = []

        def stage(*args):
            order.append("stage")
            raise ArchiveError("stage rejected")

        with patch.object(deployment, "check_packages", return_value="opkg"), \
             patch.object(deployment, "_install"), \
             patch.object(deployment, "_health", return_value=True), \
             patch.object(deployment, "install_packages", side_effect=lambda plan: order.append("install")), \
             patch.object(deployment, "_stage", side_effect=stage), \
             patch.object(deployment, "_recover", return_value=[]):
            with self.assertRaises(ArchiveError) as error:
                deployment.deploy([plan], "none")
        self.assertEqual(order, ["install", "stage"])
        self.assertIn("Packages and dependencies are not rolled back", str(error.exception))

    def test_package_preflight_failure_prevents_all_installs(self):
        plan = deployment.NodePlan(self.node, (), ("missing",), "apk")
        with patch.object(deployment, "check_packages", side_effect=ArchiveError("unavailable")), \
             patch.object(deployment, "_install") as install, \
             patch.object(deployment, "install_packages") as packages:
            with self.assertRaises(ArchiveError):
                deployment.deploy([plan], "none")
        install.assert_not_called()
        packages.assert_not_called()


if __name__ == "__main__":
    unittest.main()
