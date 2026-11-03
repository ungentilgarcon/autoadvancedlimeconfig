#!/usr/bin/env python3
"""Exercise package rollout on isolated APK and OPKG QEMU nodes."""

import argparse
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from http.server import HTTPServer
from pathlib import Path

import lime_testbed as t
from run_fleet_file_scenarios import backup, require

REPO = t.ROOT.parent
sys.path.insert(0, str(REPO))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk-image", type=Path, required=True)
    parser.add_argument("--opkg-image", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="fleet-package-qemu-") as directory:
        work = Path(directory)
        t.STATE = work / "ssh"
        tb = t.Testbed("packages-" + uuid.uuid4().hex[:8])
        for name, image in (("a", args.apk_image), ("b", args.opkg_image)):
            if not image.is_file():
                parser.error(f"image not found: {image}")
            vm = tb.add_vm(name, image.resolve(), [f"mesh{name}", "peer"], memory=448)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                vm.ssh_port = sock.getsockname()[1]
        server = None
        server_thread = None
        old_config = os.environ.get("LIBREMESH_SSH_CONFIG")
        try:
            tb.start()
            for index, name in enumerate("ab", 1):
                vm = tb.vms[name]
                vm.sh(
                    f"ip link set eth1 nomaster 2>/dev/null || true; "
                    f"ip addr add 10.77.0.{index}/24 dev eth1; ip link set eth1 up; "
                    f"ip route replace default via 10.0.2.2 dev {vm.mgmt_nic}; "
                    "echo 'nameserver 10.0.2.3' > /tmp/resolv.conf"
                )
                manager = "apk" if name == "a" else "opkg"
                vm.sh(f"{manager} update >/tmp/package-update.log 2>&1", timeout=240)
                require(vm.sh("command -v htop >/dev/null || echo ABSENT") == "ABSENT",
                        f"{name}: htop is already installed")
            for name, address in (("a", "10.77.0.2"), ("b", "10.77.0.1")):
                require(tb.vms[name].sh(f"ping -c 2 -W 2 {address} >/dev/null && echo PASS"
                                       ).endswith("PASS"), f"{name}: no peer connectivity")
            tb.setup_ssh()
            os.environ["LIBREMESH_SSH_CONFIG"] = str(tb.ssh_config)
            spec = importlib.util.spec_from_file_location("fleet_config_ui", REPO / "fleet-config-ui.py")
            ui = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ui)
            ui.deployment.fleet.dl.DEFAULT_PASS_FILE = work / "absent.pass"
            server = HTTPServer(("127.0.0.1", 0), ui.make_handler("package-test"))
            server_thread = threading.Thread(target=server.serve_forever)
            server_thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"
            inventory = work / "inventory.json"
            inventory.write_text(json.dumps({"nodes": [
                {"name": name, "host": name, "mesh_ip": f"10.77.0.{index}"}
                for index, name in enumerate("ab", 1)
            ]}), encoding="utf-8")

            def post(action, payload, expected=200):
                request = urllib.request.Request(
                    origin + "/package-test/" + action, data=json.dumps(payload).encode(),
                    headers={"Origin": origin, "Content-Type": "application/json"},
                )
                try:
                    response = urllib.request.urlopen(request, timeout=600)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    status, result = response.status, json.load(response)
                require(status == expected, f"{action}: HTTP {status}: {result}")
                return result

            before = {
                "a": tb.vms["a"].sh("sha256sum /lib/apk/db/installed /etc/apk/world"),
                "b": tb.vms["b"].sh("sha256sum /usr/lib/opkg/status"),
            }
            payload = {
                "original": backup([]),
                "edited": backup([("opt/fleet-demo/package-test", b"installed\n", 0o600)]),
                "packages": "htop", "selected": ["/opt/fleet-demo/package-test"],
                "inventory": str(inventory), "mode": "none",
            }
            plan = post("dry-run", payload)
            require(plan["package_managers"] == ["a: apk", "b: opkg"],
                    "dry run did not identify both package managers")
            for name, vm in tb.vms.items():
                files = "/lib/apk/db/installed /etc/apk/world" if name == "a" else "/usr/lib/opkg/status"
                require(vm.sh(f"sha256sum {files}") == before[name],
                        f"{name}: dry run changed package database")
                require(vm.sh("test ! -e /opt/fleet-demo/package-test && "
                              "! command -v htop >/dev/null && echo PASS").endswith("PASS"),
                        f"{name}: dry run changed node")
            post("deploy", {"plan_id": plan["plan_id"], "confirmation": "yes"}, 400)
            tb.vms["b"].sh(
                "cp /etc/opkg/distfeeds.conf /tmp/distfeeds.original; "
                "sed -i 's|^src/gz openwrt_packages .*|"
                "src/gz openwrt_packages http://127.0.0.1:9/broken|' /etc/opkg/distfeeds.conf"
            )
            failure = post("deploy", {"plan_id": plan["plan_id"], "confirmation": "DEPLOY"}, 400)
            require("attempted on: a, b" in failure["error"] and
                    "completed on: a" in failure["error"] and
                    "not rolled back" in failure["error"], "partial installation was not reported")
            for name, vm in tb.vms.items():
                require(vm.sh("test ! -e /opt/fleet-demo/package-test && echo PASS").endswith("PASS"),
                        f"{name}: file deployed despite package failure")
            require(tb.vms["a"].sh("command -v htop >/dev/null && echo PASS").endswith("PASS"),
                    "successful partial install was not retained")
            tb.vms["b"].sh("cp /tmp/distfeeds.original /etc/opkg/distfeeds.conf")
            t.log("PASS: failed second-node install reports partial packages and skips file rollout")
            plan = post("dry-run", payload)
            post("deploy", {"plan_id": plan["plan_id"], "confirmation": "DEPLOY"})
            for name, vm in tb.vms.items():
                require(vm.sh("command -v htop >/dev/null && "
                              "grep -q installed /opt/fleet-demo/package-test && echo PASS"
                              ).endswith("PASS"), f"{name}: combined rollout failed")
            t.log("PASS: mixed APK/OPKG fleet installed packages before staged file changes")

            payload.update({"original": None, "edited": None, "selected": [], "packages": "jq"})
            plan = post("dry-run", payload)
            post("deploy", {"plan_id": plan["plan_id"], "confirmation": "DEPLOY"})
            for name, vm in tb.vms.items():
                require(vm.sh("command -v jq >/dev/null && echo PASS").endswith("PASS"),
                        f"{name}: package-only rollout failed")
            t.log("PASS: package-only deployment without backups")
            return 0
        finally:
            if server:
                server.shutdown()
                server_thread.join()
                server.server_close()
            tb.stop()
            if old_config is None:
                os.environ.pop("LIBREMESH_SSH_CONFIG", None)
            else:
                os.environ["LIBREMESH_SSH_CONFIG"] = old_config


if __name__ == "__main__":
    sys.exit(main())
