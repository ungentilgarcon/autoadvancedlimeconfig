import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fleet_radio_recovery as radio
from fleet_archive import ArchiveError, Entry


class RadioRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [radio.deployment.fleet.Node({
            "name": name, "host": f"root@192.0.2.{index}", "mesh_ip": f"192.0.2.{index}",
        }) for index, name in enumerate(("a", "b"), 1)]
        self.settings = Entry("file", b"config settings 'settings'\n option enabled '0'\n", 0o600)

    @staticmethod
    def inspect(nodes):
        return [{"status": "available", "board": radio.BOARD, "hostname": node.name}
                for node in nodes]

    def test_prepare_preserves_existing_settings_and_includes_executables(self):
        def read(node, path):
            return self.settings if node.name == "a" and path == radio.CONFIG else None
        with patch.object(radio.deployment, "read_node_file", side_effect=read), \
             patch.object(radio.deployment.fleet, "remote",
                          return_value=subprocess.CompletedProcess([], 0, "", "")):
            plans, hostnames = radio.prepare(self.nodes, self.inspect)
        self.assertEqual(hostnames, {"a": "a", "b": "b"})
        self.assertEqual(plans[0].files[0].after, self.settings)
        self.assertEqual(plans[1].files[0].after.mode, 0o644)
        for plan in plans:
            self.assertEqual([item.after.mode for item in plan.files[1:]], [0o755, 0o755])
            self.assertEqual(len(plan.files), 3)

    def test_prepare_rejects_wrong_boards_and_duplicate_ips_before_reading(self):
        with patch.object(radio.deployment, "read_node_file") as read:
            with self.assertRaisesRegex(ArchiveError, "AP-303"):
                radio.prepare(self.nodes, lambda nodes: [
                    {"status": "available", "board": "other", "hostname": node.name}
                    for node in nodes
                ])
            self.nodes[1].mesh_ip = self.nodes[0].mesh_ip
            with self.assertRaisesRegex(ArchiveError, "distinct"):
                radio.prepare(self.nodes, self.inspect)
        read.assert_not_called()

    def test_prepare_rejects_file_changes_during_preflight(self):
        current = Entry("file", b"changed", 0o644)
        def read(node, path):
            read.calls += 1
            return None if read.calls <= 3 else current
        read.calls = 0
        with patch.object(radio.deployment, "read_node_file", side_effect=read), \
             patch.object(radio.deployment.fleet, "remote",
                          return_value=subprocess.CompletedProcess([], 0, "", "")), \
             self.assertRaises(ArchiveError):
            radio.prepare(self.nodes[:1], self.inspect)

    def test_activation_respects_disabled_and_stops_on_error(self):
        with patch.object(radio.deployment, "_call", side_effect=["service output\nstarted\n", "disabled\n"]) as call:
            started, disabled = radio.activate(self.nodes)
        self.assertEqual((started, disabled), (["a"], ["b"]))
        self.assertIn("flock -n 9", call.call_args_list[0].args[1])
        self.assertIn("picot-radio-recovery restart", call.call_args_list[0].args[1])
        with patch.object(radio.deployment, "_call", side_effect=ArchiveError("unavailable")) as call:
            with self.assertRaisesRegex(ArchiveError, "files were deployed.*not attempted on: b"):
                radio.activate(self.nodes)
            self.assertEqual(call.call_count, 1)

    def test_activation_shell_respects_enabled_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = root / "bin"
            commands.mkdir()
            uci = commands / "uci"
            uci.write_text('#!/bin/sh\nprintf "%s\\n" "$RADIO_ENABLED"\n')
            uci.chmod(0o755)
            service = root / "service"
            service.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$RADIO_LOG"\n')
            service.chmod(0o755)
            log = root / "service.log"
            def execute(node, command, timeout=60):
                command = command.replace("/root/libremesh-failsafe/operation.lock", str(root / "lock"))
                command = command.replace("/root/libremesh-failsafe/cfg-pending", str(root / "pending"))
                command = command.replace("/etc/init.d/picot-radio-recovery", str(service))
                env = {**os.environ, "PATH": f"{commands}:{os.environ['PATH']}",
                       "RADIO_ENABLED": "1" if node.name == "a" else "0", "RADIO_LOG": str(log)}
                result = subprocess.run(["sh", "-c", command], env=env, text=True,
                                        capture_output=True, timeout=timeout, check=False)
                if result.returncode:
                    raise ArchiveError(result.stderr)
                return result.stdout
            with patch.object(radio.deployment, "_call", side_effect=execute):
                self.assertEqual(radio.activate(self.nodes), (["a"], ["b"]))
            self.assertEqual(log.read_text().splitlines(), ["enable", "restart"])


if __name__ == "__main__":
    unittest.main()
