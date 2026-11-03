#!/usr/bin/env python3
"""Exercise the browser file-deployment workflow against isolated QEMU nodes."""

import argparse
import base64
import importlib.util
import io
import json
import os
import socket
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import HTTPServer
from pathlib import Path

import lime_testbed as t
import run_scenarios as scenarios

REPO = t.ROOT.parent
sys.path.insert(0, str(REPO))


def require(condition, message):
    if not condition:
        raise t.TestbedError(message)


def backup(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, content, mode in files:
            info = tarfile.TarInfo(path)
            info.size = len(content)
            info.mode = mode
            archive.addfile(info, io.BytesIO(content))
    return base64.b64encode(buffer.getvalue()).decode()


def config(name, setting):
    filler = "".join(f"\toption filler{i} 'value{i}'\n" for i in range(10))
    return (f"config service 'main'\n\toption node '{name}'\n{filler}"
            f"\toption setting '{setting}'\n").encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    args = parser.parse_args()
    image = args.image.resolve()
    if not image.is_file():
        parser.error(f"image not found: {image}")

    with tempfile.TemporaryDirectory(prefix="fleet-files-qemu-") as directory:
        work = Path(directory)
        t.STATE = work / "ssh"
        tb = t.Testbed("files-" + uuid.uuid4().hex[:8])
        for name in "abc":
            vm = tb.add_vm(name, image, ["mesh", f"wan{name}"], memory=448)
            vm.allow_reboot = name == "a"
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                vm.ssh_port = sock.getsockname()[1]
        server = None
        server_thread = None
        original_env = os.environ.get("LIBREMESH_SSH_CONFIG")
        try:
            tb.start()
            scenarios.prepare(tb)
            for index, name in enumerate("abc", 1):
                vm = tb.vms[name]
                vm.sh(f"ip addr del 10.77.0.{index}/24 dev bat0; "
                      f"ip addr add 10.77.0.{index}/24 dev br-lan")
                vm.serial.put_file("/etc/config/fleetcheck", config(name, "before"), "600")
            time.sleep(30)
            for source in "abc":
                for index, target in enumerate("abc", 1):
                    if source == target:
                        continue
                    require(scenarios.wait_for(
                        lambda: tb.vms[source].sh(
                            f"ping -c 2 -W 2 10.77.0.{index} >/dev/null && echo PASS",
                            check=False,
                        ).endswith("PASS"), 120, 3),
                        f"mesh ping {source}->{target} failed")

            os.environ["LIBREMESH_SSH_CONFIG"] = str(tb.ssh_config)
            spec = importlib.util.spec_from_file_location("fleet_config_ui", REPO / "fleet-config-ui.py")
            ui = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ui)
            ui.deployment.fleet.dl.DEFAULT_PASS_FILE = work / "absent.pass"
            server = HTTPServer(("127.0.0.1", 0), ui.make_handler("qemu-test"))
            server_thread = threading.Thread(target=server.serve_forever)
            server_thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"
            inventory = work / "inventory.json"
            inventory.write_text(json.dumps({"nodes": [
                {"name": name, "host": name, "mesh_ip": f"10.77.0.{index}"}
                for index, name in enumerate("abc", 1)
            ]}), encoding="utf-8")

            def post(action, payload, expected=200):
                request = urllib.request.Request(
                    origin + "/qemu-test/" + action, data=json.dumps(payload).encode(),
                    headers={"Origin": origin, "Content-Type": "application/json"},
                )
                try:
                    response = urllib.request.urlopen(request, timeout=600)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    status = response.status
                    result = json.load(response)
                require(status == expected, f"{action}: HTTP {status}: {result}")
                return result

            payload = {
                "original": backup([("etc/config/fleetcheck", config("a", "before"), 0o600)]),
                "edited": backup([
                    ("etc/config/fleetcheck", config("a", "after"), 0o600),
                    ("usr/sbin/fleet-test-tool", b"#!/bin/sh\nexit 0\n", 0o755),
                    ("opt/fleet-demo/tool", b"#!/bin/sh\nexit 0\n", 0o755),
                ]),
                "packages": "",
                "selected": ["/etc/config/fleetcheck", "/usr/sbin/fleet-test-tool",
                             "/opt/fleet-demo/tool"],
                "inventory": str(inventory),
                "mode": "none",
            }
            plan = post("dry-run", payload)
            for vm in tb.vms.values():
                require(vm.sh("uci -q get fleetcheck.main.setting") == "before",
                        "dry run changed UCI configuration")
                require(vm.sh("test ! -e /usr/sbin/fleet-test-tool && "
                              "test ! -e /opt/fleet-demo/tool && echo PASS").endswith("PASS"),
                        "dry run created a file")
            post("deploy", {"plan_id": plan["plan_id"], "confirmation": "yes"}, 400)
            t.log("PASS: dry run did not change nodes; missing confirmation rejected")
            result = post("deploy", {"plan_id": plan["plan_id"], "confirmation": "DEPLOY"})
            for name, vm in tb.vms.items():
                require(vm.sh("uci -q get fleetcheck.main.setting") == "after",
                        f"{name}: configuration not changed")
                require(vm.sh("uci -q get fleetcheck.main.node") == name,
                        f"{name}: node-specific setting lost")
                require(vm.sh("test -x /usr/sbin/fleet-test-tool && "
                              "test -x /opt/fleet-demo/tool && echo PASS").endswith("PASS"),
                        f"{name}: deployed tools missing or not executable")
                require("state=confirmed" in vm.sh(
                    f"/usr/sbin/picot-files-sync status {result['change_id']}"),
                    f"{name}: not confirmed")
            t.log("PASS: fleet deployment preserves per-node values and installs executable files")

            stale = {
                **payload,
                "original": backup([("etc/config/fleetcheck", config("a", "after"), 0o600)]),
                "edited": backup([("etc/config/fleetcheck", config("a", "next"), 0o600)]),
                "selected": ["/etc/config/fleetcheck"],
            }
            plan = post("dry-run", stale)
            tb.vms["b"].sh("uci set fleetcheck.main.setting=manual; uci commit fleetcheck")
            post("deploy", {"plan_id": plan["plan_id"], "confirmation": "DEPLOY"}, 400)
            require(tb.vms["a"].sh("uci -q get fleetcheck.main.setting") == "after",
                    "stale deployment changed node a")
            require(tb.vms["b"].sh("uci -q get fleetcheck.main.setting") == "manual",
                    "stale deployment overwrote node b")
            require(tb.vms["c"].sh("uci -q get fleetcheck.main.setting") == "after",
                    "stale deployment changed node c")
            t.log("PASS: changed target aborts staging without overwriting nodes")

            from fleet_archive import Change, Entry

            nodes, _ = ui.deployment.fleet.load_inventory(inventory)
            node = nodes[0]
            tb.vms["a"].sh("chown 123:456 /opt/fleet-demo/tool")
            old_tool = Entry("file", b"#!/bin/sh\nexit 0\n", 0o755)
            changes = [
                Change("usr/sbin/fleet-test-tool", old_tool, None),
                Change("opt/fleet-demo/tool", old_tool,
                       Entry("file", b"#!/bin/sh\nexit 1\n", 0o700)),
                Change("opt/fleet-demo/new", None, Entry("file", b"new\n", 0o600)),
            ]
            plans = ui.deployment.preflight([node], changes)
            with tempfile.TemporaryDirectory(prefix="fleet-qemu-upload-") as upload:
                ui.deployment._stage(plans[0], "watchdog-test", "none", Path(upload))
            ui.deployment._call(node, "/usr/sbin/picot-files-sync apply-now watchdog-test 15")
            vm = tb.vms["a"]
            require(vm.sh("test ! -e /usr/sbin/fleet-test-tool && "
                          "test -f /opt/fleet-demo/new && "
                          "ls -ldn /opt/fleet-demo/tool | "
                          "awk '{if ($3 != 123 || $4 != 456) exit 1}' && echo PASS").endswith("PASS"),
                    "watchdog test did not apply additions and deletions")
            require(scenarios.wait_for(
                lambda: "state=rolledback" in vm.sh(
                    "/usr/sbin/picot-files-sync status watchdog-test"), 45, 2),
                "watchdog did not roll back")
            require(vm.sh("test -x /usr/sbin/fleet-test-tool && "
                          "test -x /opt/fleet-demo/tool && "
                          "test \"$(ls -ldn /opt/fleet-demo/tool | cut -c1-10)\" = -rwxr-xr-x && "
                          "ls -ldn /opt/fleet-demo/tool | "
                          "awk '{if ($3 != 123 || $4 != 456) exit 1}' && "
                          "grep -q 'exit 0' /opt/fleet-demo/tool && "
                          "test ! -e /opt/fleet-demo/new && echo PASS").endswith("PASS"),
                    "watchdog did not restore files and executable modes")
            t.log("PASS: watchdog restores modified and deleted files and removes additions")

            addition = Change("opt/fleet-demo/concurrent", None, Entry("file", b"test\n", 0o600))
            plans = ui.deployment.preflight([node], [addition])
            with tempfile.TemporaryDirectory(prefix="fleet-qemu-upload-") as upload:
                ui.deployment._stage(plans[0], "cancel-test", "none", Path(upload))
            vm.sh('now=$(date +%s); /usr/sbin/picot-files-sync apply-at cancel-test "$((now+8))" 30; '
                  '/usr/sbin/picot-files-sync cancel cancel-test')
            time.sleep(10)
            require(vm.sh("test ! -e /opt/fleet-demo/concurrent && "
                          "grep -q '^cancelled$' /root/libremesh-failsafe/files-cancel-test/state "
                          "&& echo PASS").endswith("PASS"),
                    "cancelled scheduled change was applied")
            t.log("PASS: cancellation prevents the scheduled apply")

            plans = ui.deployment.preflight([node], [addition])
            with tempfile.TemporaryDirectory(prefix="fleet-qemu-upload-") as upload:
                ui.deployment._stage(plans[0], "cross-files", "none", Path(upload))
            vm.serial.put_file("/tmp/cross-uci", b"set fleetcheck.main.setting=concurrent\n", "600")
            vm.sh("/usr/sbin/picot-config-sync stage cross-uci /tmp/cross-uci none")
            result = vm.sh(
                "/usr/sbin/picot-files-sync apply-now cross-files 10 >/tmp/cross-files.log 2>&1 & "
                "p=$!; /usr/sbin/picot-config-sync apply-now cross-uci 10 >/tmp/cross-uci.log 2>&1 & "
                "q=$!; wait \"$p\"; a=$?; wait \"$q\"; b=$?; "
                "if { [ \"$a\" -eq 0 ] && [ \"$b\" -ne 0 ]; } || "
                "{ [ \"$b\" -eq 0 ] && [ \"$a\" -ne 0 ]; }; then echo PASS; else echo FAIL; fi"
            )
            require(result.endswith("PASS"), "competing file and UCI changes did not serialize")
            require(scenarios.wait_for(
                lambda: vm.sh("test ! -s /root/libremesh-failsafe/cfg-pending && echo PASS",
                              check=False).endswith("PASS"), 45, 2),
                "competing change did not roll back")
            require(vm.sh("test ! -e /opt/fleet-demo/concurrent && "
                          "test \"$(uci -q get fleetcheck.main.setting)\" = after && echo PASS"
                          ).endswith("PASS"),
                    "competing change left altered files or configuration")
            t.log("PASS: concurrent file and UCI changes serialize and roll back")

            race_files = []
            for index in range(12):
                change = Change(f"opt/fleet-demo/race-{index}", None, Entry("file", b"x\n", 0o600))
                race_files.append(ui.deployment.PlannedFile(change, None, change.edited))
            race_plan = ui.deployment.NodePlan(node, tuple(race_files))
            with tempfile.TemporaryDirectory(prefix="fleet-qemu-upload-") as upload:
                ui.deployment._stage(race_plan, "race-test", "none", Path(upload))
            vm.sh("mkdir -p /tmp/race-bin")
            vm.serial.put_file("/tmp/race-bin/mktemp",
                               b'#!/bin/sh\nsleep 1\nexec /bin/busybox mktemp "$@"\n', "755")
            overlap = vm.sh(
                "PATH=/tmp/race-bin:$PATH /usr/sbin/picot-files-sync apply-now race-test 60 "
                ">/tmp/race-apply.log 2>&1 & "
                "p=$!; seen=0; n=0; while [ \"$n\" -lt 30 ]; do n=$((n+1)); "
                "if [ \"$(cat /root/libremesh-failsafe/files-race-test/state)\" = applying ]; "
                "then seen=1; break; fi; sleep 1; done; "
                "if [ \"$seen\" = 1 ]; then "
                "/usr/sbin/picot-files-sync rollback race-test "
                ">/tmp/race-rollback.log 2>&1 & q=$!; "
                "wait \"$p\" && wait \"$q\" && echo PASS; "
                "else wait \"$p\"; echo FAIL; fi"
            )
            require(overlap.endswith("PASS"),
                    "concurrent apply and rollback failed: " + overlap + "; "
                    + vm.sh("cat /tmp/race-apply.log /tmp/race-rollback.log 2>/dev/null; true"))
            time.sleep(3)
            require(vm.sh("test -z \"$(find /opt/fleet-demo -name 'race-*')\" && echo PASS"
                          ).endswith("PASS"), "apply wrote files after rollback")
            t.log("PASS: rollback waits for apply and no files reappear")

            with tempfile.TemporaryDirectory(prefix="fleet-qemu-upload-") as upload:
                ui.deployment._stage(race_plan, "expire-test", "none", Path(upload))
            require(vm.sh(
                "if PATH=/tmp/race-bin:$PATH /usr/sbin/picot-files-sync "
                "apply-now expire-test 3 >/tmp/expire.log 2>&1; then echo FAIL; "
                "else echo PASS; fi"
            ).endswith("PASS"), "expired apply was accepted")
            require(vm.sh("test -z \"$(find /opt/fleet-demo -name 'race-*')\" && "
                          "grep -q '^rolledback$' /root/libremesh-failsafe/files-expire-test/state "
                          "&& echo PASS").endswith("PASS"), "expired apply was not rolled back")
            t.log("PASS: an apply that exceeds its deadline rolls back")

            plans = ui.deployment.preflight([node], [addition])
            with tempfile.TemporaryDirectory(prefix="fleet-qemu-upload-") as upload:
                ui.deployment._stage(plans[0], "extend-test", "none", Path(upload))
            vm.sh("/usr/sbin/picot-files-sync apply-now extend-test 3")
            vm.sh("exec 8>/root/libremesh-failsafe/operation.lock; flock -x 8; sleep 5; "
                  "echo $(( $(cut -d. -f1 /proc/uptime) + 10 )) "
                  "> /root/libremesh-failsafe/files-extend-test/deadline; "
                  "flock -u 8; exec 8>&-")
            require("state=applied" in vm.sh("/usr/sbin/picot-files-sync status extend-test"),
                    "watchdog ignored an extended deadline")
            require(scenarios.wait_for(
                lambda: "state=rolledback" in vm.sh(
                    "/usr/sbin/picot-files-sync status extend-test"), 35, 2),
                "watchdog stopped after observing an extended deadline")
            t.log("PASS: watchdog rechecks an extended deadline while waiting for the lock")

            plans = ui.deployment.preflight([node], [addition])
            with tempfile.TemporaryDirectory(prefix="fleet-qemu-upload-") as upload:
                ui.deployment._stage(plans[0], "boot-test", "none", Path(upload))
            vm.sh("/usr/sbin/picot-files-sync apply-now boot-test 120; sync")
            vm.monitor("system_reset")
            time.sleep(10)
            vm.serial.wait_shell(timeout=180)
            require(scenarios.wait_for(
                lambda: "state=rolledback" in vm.sh("/usr/sbin/picot-files-sync status boot-test"),
                60, 2), "boot recovery did not roll back the pending change")
            require(vm.sh("test ! -e /opt/fleet-demo/concurrent && echo PASS").endswith("PASS"),
                    "boot recovery left the added file")
            t.log("PASS: reboot recovery rolls back a pending file change")
            return 0
        finally:
            if server:
                server.shutdown()
                server_thread.join()
                server.server_close()
            tb.stop()
            if original_env is None:
                os.environ.pop("LIBREMESH_SSH_CONFIG", None)
            else:
                os.environ["LIBREMESH_SSH_CONFIG"] = original_env


if __name__ == "__main__":
    sys.exit(main())
