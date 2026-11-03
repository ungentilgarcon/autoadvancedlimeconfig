import base64
import fcntl
import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import fleet_firmware as firmware
from fleet_archive import ArchiveError


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [
            firmware.fleet.Node({"name": "a", "host": "root@192.0.2.1", "mesh_ip": "192.0.2.1"}),
            firmware.fleet.Node({"name": "b", "host": "root@192.0.2.2", "mesh_ip": "192.0.2.2",
                                 "canary": True}),
        ]
        self.image = b"example firmware"
        self.encoded = base64.b64encode(self.image).decode()

    @staticmethod
    def inspect(nodes):
        return [{"name": node.name, "status": "available", "board": "aruba,ap-303",
                 "hostname": node.name} for node in nodes]

    def test_decode_accepts_bin_and_itb_and_rejects_invalid_input(self):
        for name in ("firmware image.bin", "openwrt.itb", "image.BIN"):
            self.assertEqual(firmware.decode_image(name, self.encoded), (name, self.image))
        for name in ("", "firmware.tib", "other.gz", "../firmware.bin", "a\\b.itb"):
            with self.subTest(name=name), self.assertRaises(ArchiveError):
                firmware.decode_image(name, self.encoded)
        for data in ("", "!!!"):
            with self.subTest(data=data), self.assertRaises(ArchiveError):
                firmware.decode_image("image.bin", data)
        with patch.object(firmware, "MAX_IMAGE_BYTES", 4):
            self.assertEqual(firmware.decode_image("image.bin", base64.b64encode(b"1234").decode())[1], b"1234")
            with self.assertRaises(ArchiveError):
                firmware.decode_image("image.bin", base64.b64encode(b"12345").decode())

    def test_prepare_rejects_mixed_models_without_uploading(self):
        def inspect(nodes):
            result = self.inspect(nodes)
            result[1]["board"] = "xiaomi,other"
            return result
        with patch.object(firmware.fleet, "push") as push:
            with self.assertRaisesRegex(ArchiveError, "same live board"):
                firmware.prepare(self.nodes, "image.bin", self.encoded, inspect)
        push.assert_not_called()

    def test_prepare_checks_every_node_before_exposing_plan(self):
        commands = []
        def remote(node, command, timeout=60):
            commands.append((node.name, command))
            if " health " in command:
                return subprocess.CompletedProcess([], 0, "health=ok\npeers_ok=1\npeers_total=1\n", "")
            return subprocess.CompletedProcess([], 0, "", "")
        with patch.object(firmware.fleet, "remote", side_effect=remote), \
             patch.object(firmware.fleet, "push", return_value=True) as push:
            plan = firmware.prepare(self.nodes, "image.itb", self.encoded, self.inspect)
        self.assertEqual([node.name for node in plan.nodes], ["b", "a"])
        self.assertEqual(plan.digest, hashlib.sha256(self.image).hexdigest())
        self.assertEqual(sum("sysupgrade -T " in cmd for _, cmd in commands), 2)
        self.assertEqual(sum(cmd.startswith("rm -f -- ") for _, cmd in commands), 4)
        self.assertEqual(sum("command -v flock" in cmd for _, cmd in commands), 2)
        self.assertEqual(push.call_count, 4)
        self.assertFalse(any("sysupgrade -c" in cmd for _, cmd in commands))

    def test_prepare_rejects_failed_image_and_cleans_up(self):
        commands = []
        def remote(node, command, timeout=60):
            commands.append(command)
            if " health " in command:
                return subprocess.CompletedProcess([], 0, "health=ok\npeers_ok=1\npeers_total=1\n", "")
            return subprocess.CompletedProcess([], 1 if "sysupgrade -T" in command else 0, "", "rejected")
        with patch.object(firmware.fleet, "remote", side_effect=remote), \
             patch.object(firmware.fleet, "push", return_value=True):
            with self.assertRaisesRegex(ArchiveError, "rejected"):
                firmware.prepare(self.nodes, "image.bin", self.encoded, self.inspect)
        self.assertTrue(any(command.startswith("rm -f -- ") for command in commands))
        self.assertFalse(any("sysupgrade -c" in command for command in commands))

    def test_flash_checks_identity_image_and_reboot_before_success(self):
        node = self.nodes[0]
        plan = firmware.FirmwarePlan(self.image, "image.bin", hashlib.sha256(self.image).hexdigest(),
                                     "aruba,ap-303", {"a": "a"}, (node,))
        commands = []
        def remote(node, command, timeout=60):
            commands.append(command)
            return subprocess.CompletedProcess([], 0, "", "")
        with patch.object(firmware, "_health", return_value=True), \
             patch.object(firmware, "_boot_id", side_effect=["old", "new"]), \
             patch.object(firmware.fleet, "remote", side_effect=remote), \
             patch.object(firmware.fleet, "push", return_value=True), \
             patch.object(firmware.fleet, "wait_for_reboot", return_value=True):
            firmware.flash_node(node, plan, self.inspect)
        image_test = next(i for i, command in enumerate(commands) if "sysupgrade -T" in command)
        flash = next(i for i, command in enumerate(commands) if "sysupgrade -c" in command)
        self.assertLess(image_test, flash)
        self.assertLess(commands[flash].index("sysupgrade -T"), commands[flash].index("sysupgrade -c"))
        self.assertIn("flock -n 9", commands[flash])
        self.assertNotIn("nohup", commands[flash])
        self.assertIn("trap '' HUP", commands[flash])

    @unittest.skipUnless(shutil.which("busybox") and shutil.which("flock"), "BusyBox and flock required")
    def test_background_launch_works_without_nohup_and_survives_hangup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bindir = root / "bin"
            bindir.mkdir()
            (bindir / "sleep").symlink_to(shutil.which("busybox"))
            upgrade = bindir / "sysupgrade"
            upgrade.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TEST_MARKER"\nsleep 1\n')
            upgrade.chmod(0o755)
            marker, lock, log = root / "started", root / "lock", root / "upgrade.log"
            image = "/tmp/image with spaces.bin"
            command = firmware.fleet.dl.sysupgrade_background_command(image)
            command = command.replace("/tmp/libremesh-sysupgrade.log", str(log))
            command = f"exec 9>{lock}; {shutil.which('flock')} -n 9; " + command
            env = {**os.environ, "PATH": str(bindir), "TEST_MARKER": str(marker)}
            process = subprocess.Popen([shutil.which("busybox"), "ash", "-c", command],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, (stdout, stderr))
            os.killpg(process.pid, signal.SIGHUP)
            with lock.open("w") as lock_file:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), log.read_text() if log.exists() else "no launch log")
            self.assertEqual(marker.read_text().splitlines(), ["-c", "-v", image])
            deadline = time.monotonic() + 5
            with lock.open("w") as lock_file:
                while True:
                    try:
                        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            self.fail("background process did not release its lock")
                        time.sleep(0.05)

    def test_flash_does_not_launch_when_identity_or_image_test_fails(self):
        node = self.nodes[0]
        plan = firmware.FirmwarePlan(self.image, "image.bin", hashlib.sha256(self.image).hexdigest(),
                                     "aruba,ap-303", {"a": "a"}, (node,))
        with patch.object(firmware.fleet, "push") as push:
            with self.assertRaisesRegex(ArchiveError, "board or hostname"):
                firmware.flash_node(node, plan, lambda nodes: [{
                    "status": "available", "board": "other,board", "hostname": "a",
                }])
            push.assert_not_called()
        commands = []
        def remote(node, command, timeout=60):
            commands.append(command)
            return subprocess.CompletedProcess([], 1 if "sysupgrade -T" in command else 0, "", "rejected")
        with patch.object(firmware, "_health", return_value=True), \
             patch.object(firmware, "_boot_id", return_value="old"), \
             patch.object(firmware.fleet, "remote", side_effect=remote), \
             patch.object(firmware.fleet, "push", return_value=True):
            with self.assertRaisesRegex(ArchiveError, "rejected"):
                firmware.flash_node(node, plan, self.inspect)
        self.assertFalse(any("sysupgrade -c" in command for command in commands))
        self.assertTrue(any(command.startswith("rm -f -- ") for command in commands))

    def test_uncertain_launch_does_not_remove_firmware(self):
        node = self.nodes[0]
        plan = firmware.FirmwarePlan(self.image, "image.bin", hashlib.sha256(self.image).hexdigest(),
                                     "aruba,ap-303", {"a": "a"}, (node,))
        commands = []
        def remote(node, command, timeout=60):
            commands.append(command)
            return subprocess.CompletedProcess([], 255 if "sysupgrade -c" in command else 0, "", "lost SSH")
        with patch.object(firmware, "_health", return_value=True), \
             patch.object(firmware, "_boot_id", return_value="old"), \
             patch.object(firmware.fleet, "remote", side_effect=remote), \
             patch.object(firmware.fleet, "push", return_value=True):
            with self.assertRaisesRegex(ArchiveError, "outcome is uncertain"):
                firmware.flash_node(node, plan, self.inspect)
        self.assertFalse(any(command.startswith("rm -f -- ") for command in commands))

    def test_unchanged_boot_id_never_reports_success(self):
        node = self.nodes[0]
        plan = firmware.FirmwarePlan(self.image, "image.bin", hashlib.sha256(self.image).hexdigest(),
                                     "aruba,ap-303", {"a": "a"}, (node,))
        with patch.object(firmware, "_health", return_value=True), \
             patch.object(firmware, "_boot_id", return_value="unchanged"), \
             patch.object(firmware.fleet, "remote", return_value=subprocess.CompletedProcess([], 0, "", "")), \
             patch.object(firmware.fleet, "push", return_value=True), \
             patch.object(firmware.fleet, "wait_for_reboot", return_value=True):
            with self.assertRaisesRegex(ArchiveError, "boot ID did not change"):
                firmware.flash_node(node, plan, self.inspect)


if __name__ == "__main__":
    unittest.main()
