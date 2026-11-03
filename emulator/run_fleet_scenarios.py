#!/usr/bin/env python3
"""Exercise fleet configuration staging and rollback on three isolated VMs."""

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import lime_testbed as t
import run_scenarios as scenarios

REPO = t.ROOT.parent


def require(condition: bool, message: str) -> None:
    if not condition:
        raise t.TestbedError(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True,
                        help="raw x86/64 LibreMesh image")
    args = parser.parse_args()
    image = args.image.resolve()
    if not image.is_file():
        parser.error(f"image not found: {image}")

    with tempfile.TemporaryDirectory(prefix="fleet-qemu-") as directory:
        work = Path(directory)
        t.STATE = work / "ssh"
        tb = t.Testbed("fleet-" + uuid.uuid4().hex[:8])
        for name, links in (
            ("a", ["mesh", "wana"]),
            ("b", ["mesh", "wanb"]),
            ("c", ["mesh", "wanc"]),
        ):
            vm = tb.add_vm(name, image, links, memory=448)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                vm.ssh_port = sock.getsockname()[1]
        try:
            tb.start()
            scenarios.prepare(tb)
            for index, name in enumerate("abc", 1):
                tb.vms[name].sh(
                    f"ip addr del 10.77.0.{index}/24 dev bat0; "
                    f"ip addr add 10.77.0.{index}/24 dev br-lan"
                )
            inventory = work / "inventory.json"
            inventory.write_text(json.dumps({"nodes": [
                {"name": name, "host": name, "mesh_ip": f"10.77.0.{i}",
                 "canary": name == "a", "role": "gateway" if name == "c" else "node"}
                for i, name in enumerate("abc", 1)
            ]}), encoding="utf-8")
            # Give batman-adv time to converge before requiring two-packet
            # reachability, as the controller does in its health check.
            time.sleep(30)
            for source in "abc":
                for index in range(1, 4):
                    if index == "abc".index(source) + 1:
                        continue
                    vm = tb.vms[source]
                    ok = scenarios.wait_for(
                        lambda: vm.sh(
                            f"ping -c 2 -W 2 10.77.0.{index} >/dev/null && echo PASS",
                            check=False,
                        ).endswith("PASS"), 180,
                    )
                    require(ok, f"mesh ping {source} -> 10.77.0.{index} failed")
            t.log("PASS: bidirectional mesh peer reachability")

            def fleet(*command: str, expected: int = 0) -> None:
                result = subprocess.run(
                    [sys.executable, str(REPO / "fleet-libremesh.py"), *command,
                     "--inventory", str(inventory), "--pass-file", str(work / "absent.pass")],
                    env=tb.env(), capture_output=True, text=True, timeout=180, check=False,
                )
                print(result.stdout + result.stderr, flush=True)
                require(result.returncode == expected,
                        f"fleet command exited {result.returncode}, expected {expected}")

            def change(name: str, payload: dict) -> str:
                path = work / f"{name}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                return str(path)

            def verify(value: int, change_id: str | None = None) -> None:
                for vm in tb.vms.values():
                    actual = vm.sh("uci -q get system.@system[0].log_size")
                    require(actual == str(value), f"{vm.name}: expected {value}, got {actual}")
                    if change_id:
                        status = vm.sh(f"/usr/sbin/picot-config-sync status {change_id}")
                        require("state=confirmed" in status, f"{vm.name}: {status}")

            safe = change("safe", {"kind": "config-safe", "apply": "none",
                                   "uci": ["set system.@system[0].log_size=123"]})
            fleet("config", safe, "--id", "qemu-safe", "--window", "120")
            verify(123, "qemu-safe")
            t.log("PASS: safe change applied and confirmed on all VMs")

            breaking = change("breaking", {"kind": "config-breaking", "apply": "none",
                                          "uci": ["set system.@system[0].log_size=124"]})
            fleet("config", breaking, "--id", "qemu-breaking", "--lead", "15",
                  "--window", "120")
            verify(124, "qemu-breaking")
            t.log("PASS: scheduled apply and confirmation on all VMs")

            rejected = change("reject", {
                "kind": "config-breaking", "apply": "none",
                "uci": ["set system.@system[0].log_size=125"],
                "node_uci": {"b": ["set missing.section.option=1"]},
            })
            fleet("config", rejected, "--id", "qemu-reject", "--lead", "15",
                  "--window", "120", expected=1)
            verify(124)
            for vm in tb.vms.values():
                require(not vm.sh("uci changes system"), f"{vm.name}: staged UCI deltas leaked")
            t.log("PASS: invalid UCI rejected without applying or leaking staged changes")

            vm = tb.vms["a"]
            vm.sh("echo 'set system.@system[0].log_size=126' > /tmp/timeout.uci; "
                  "/usr/sbin/picot-config-sync stage qemu-timeout /tmp/timeout.uci none; "
                  "/usr/sbin/picot-config-sync apply-now qemu-timeout 8")
            require(vm.sh("uci -q get system.@system[0].log_size") == "126",
                    "watchdog test change was not applied")
            rolled_back = scenarios.wait_for(
                lambda: "state=rolledback" in vm.sh(
                    "/usr/sbin/picot-config-sync status qemu-timeout", check=False),
                45, 2,
            )
            require(rolled_back, "automatic rollback did not complete")
            require(vm.sh("uci -q get system.@system[0].log_size") == "124",
                    "watchdog did not restore the previous value")
            t.log("PASS: watchdog restores configuration after deadline")
            return 0
        finally:
            tb.stop()


if __name__ == "__main__":
    sys.exit(main())
