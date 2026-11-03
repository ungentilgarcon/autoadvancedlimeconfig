#!/usr/bin/env python3
"""Verify package-only deployment and dependencies on one isolated QEMU node."""

import argparse
import importlib.util
import json
import os
import re
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
from run_fleet_file_scenarios import require

REPO = t.ROOT.parent
sys.path.insert(0, str(REPO))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    args = parser.parse_args()
    if not args.image.is_file():
        parser.error(f"image not found: {args.image}")
    with tempfile.TemporaryDirectory(prefix="fleet-one-package-") as directory:
        work = Path(directory)
        t.STATE = work / "ssh"
        tb = t.Testbed("one-package-" + uuid.uuid4().hex[:8])
        vm = tb.add_vm("a", args.image.resolve(), ["mesh", "wan"], memory=448)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            vm.ssh_port = sock.getsockname()[1]
        server = None
        server_thread = None
        old_config = os.environ.get("LIBREMESH_SSH_CONFIG")
        try:
            tb.start()
            vm.sh(f"ip route replace default via 10.0.2.2 dev {vm.mgmt_nic}; "
                  "echo 'nameserver 10.0.2.3' > /tmp/resolv.conf; "
                  "ip addr add 10.77.0.1/24 dev br-lan; "
                  "apk update >/tmp/package-update.log 2>&1", timeout=240)
            packages = ()
            dependencies = []
            for candidate in ("git-http", "python3-light", "rsync", "socat"):
                requested = ("jq", candidate)
                simulation = vm.sh("apk --simulate add -- " + " ".join(requested),
                                   timeout=120, check=False)
                additions = re.findall(r"Installing ([A-Za-z0-9][A-Za-z0-9+_.-]*) \(", simulation)
                found = sorted(set(additions) - set(requested))
                if set(requested).issubset(additions) and found:
                    packages, dependencies = requested, found
                    break
            require(bool(packages), "no test package with a missing dependency was available")
            for package in (*packages, *dependencies):
                require(vm.sh(f"apk info -e {package} >/dev/null 2>&1 && echo PRESENT || echo MISSING")
                        == "MISSING", f"{package} was already installed")
            database = vm.sh("sha256sum /lib/apk/db/installed /etc/apk/world")
            tb.setup_ssh()
            os.environ["LIBREMESH_SSH_CONFIG"] = str(tb.ssh_config)
            spec = importlib.util.spec_from_file_location("fleet_config_ui", REPO / "fleet-config-ui.py")
            ui = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ui)
            ui.deployment.fleet.dl.DEFAULT_PASS_FILE = work / "absent.pass"
            server = HTTPServer(("127.0.0.1", 0), ui.make_handler("single-test"))
            server_thread = threading.Thread(target=server.serve_forever)
            server_thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"
            inventory = work / "inventory.json"
            inventory.write_text(json.dumps({"nodes": [
                {"name": "a", "host": "a", "mesh_ip": "10.77.0.1"},
            ]}), encoding="utf-8")

            def post(action, payload, expected=200):
                request = urllib.request.Request(
                    origin + "/single-test/" + action, data=json.dumps(payload).encode(),
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

            payload = {"original": None, "edited": None, "packages": ", ".join(packages),
                       "selected": [], "inventory": str(inventory), "mode": "none"}
            plan = post("dry-run", payload)
            require(vm.sh("sha256sum /lib/apk/db/installed /etc/apk/world") == database,
                    "dry run changed the package database")
            post("deploy", {"plan_id": plan["plan_id"], "confirmation": "yes"}, 400)
            post("deploy", {"plan_id": plan["plan_id"], "confirmation": "DEPLOY"})
            for package in (*packages, *dependencies):
                require(vm.sh(f"apk info -e {package} >/dev/null 2>&1 && echo PRESENT || echo MISSING")
                        == "PRESENT", f"{package} was not installed")
            t.log("PASS: package-only deployment installed " + ", ".join(packages)
                  + " and dependencies: " + ", ".join(dependencies))
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
