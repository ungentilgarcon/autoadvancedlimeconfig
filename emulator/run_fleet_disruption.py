#!/usr/bin/env python3
"""Test a genuinely mesh-disrupting fleet configuration in isolated QEMU VMs."""

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
HELPER = "/usr/sbin/picot-config-sync"
VLAN = "network.lm_net_eth0_batadv_dev.vid"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise t.TestbedError(message)


def ping(vm: t.VM, address: str) -> bool:
    return vm.sh(
        f"ping -c 2 -W 2 {address} >/dev/null && echo PASS",
        check=False,
    ).endswith("PASS")


def peer_mesh(tb: t.Testbed, timeout: int = 150) -> None:
    for source in "abc":
        for index in range(1, 4):
            if index == "abc".index(source) + 1:
                continue
            require(scenarios.wait_for(
                lambda: ping(tb.vms[source], f"10.77.0.{index}"), timeout, 3,
            ), f"{source} could not reach 10.77.0.{index}")


def vlan_status(vm: t.VM) -> tuple[str, str]:
    configured = vm.sh(f"uci -q get {VLAN}")
    link = vm.sh("ip -d link show eth0_29", check=False)
    return configured, link


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True,
                        help="raw x86/64 LibreMesh image")
    parser.add_argument("--only", choices=("partition", "migration"),
                        help="run just one scenario")
    args = parser.parse_args()
    image = args.image.resolve()
    if not image.is_file():
        parser.error(f"image not found: {image}")

    with tempfile.TemporaryDirectory(prefix="fleet-disruption-") as directory:
        work = Path(directory)
        t.STATE = work / "ssh"
        tb = t.Testbed("disruption-" + uuid.uuid4().hex[:8])
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
                vm = tb.vms[name]
                require(vm.mgmt_nic is not None, f"{name}: missing management interface")
                vm.sh(
                    "uci -q del_list network.@device[0].ports=eth0 || true; "
                    f"uci -q del_list network.@device[0].ports={vm.mgmt_nic} || true; "
                    "uci set network.fleettest=interface; "
                    "uci set network.fleettest.proto=static; "
                    "uci set network.fleettest.device=br-lan; "
                    f"uci set network.fleettest.ipaddr=10.77.0.{index}; "
                    "uci set network.fleettest.netmask=255.255.255.0; "
                    "uci commit network; "
                    f"ip addr del 10.77.0.{index}/24 dev bat0 2>/dev/null || true; "
                    "/etc/init.d/network reload",
                    timeout=90,
                )
                require(vm.sh("test ! -L /sys/class/net/eth0/master && echo PASS",
                              check=False).endswith("PASS"),
                        f"{name}: untagged mesh port is still bridged")
                configured, link = vlan_status(vm)
                require(configured == "29" and "id 29" in link,
                        f"{name}: unexpected initial mesh VLAN: {configured}\n{link}")
            time.sleep(30)
            peer_mesh(tb)
            t.log("PASS: initial peers reachable over batman-adv VLAN 29")

            inventory = work / "inventory.json"
            inventory.write_text(json.dumps({"nodes": [
                {"name": name, "host": name, "mesh_ip": f"10.77.0.{index}"}
                for index, name in enumerate("abc", 1)
            ]}), encoding="utf-8")

            def run_change(change_id: str, bad_node: bool) -> None:
                payload = {
                    "kind": "config-breaking", "apply": "reload",
                    "uci": [f"set {VLAN}=37"],
                }
                if bad_node:
                    payload["node_uci"] = {"b": [f"set {VLAN}=38"]}
                change = work / f"{change_id}.json"
                change.write_text(json.dumps(payload), encoding="utf-8")
                output = work / f"{change_id}.log"
                with output.open("w+", encoding="utf-8") as log:
                    process = subprocess.Popen(
                        [sys.executable, "-u", str(REPO / "fleet-libremesh.py"),
                         "config", str(change), "--inventory", str(inventory),
                         "--pass-file", str(work / "absent.pass"), "--id", change_id,
                         "--lead", "45", "--window", "240"],
                        env=tb.env(), stdout=log, stderr=subprocess.STDOUT,
                    )
                    try:
                        armed = scenarios.wait_for(
                            lambda: all("state=armed" in vm.sh(
                                f"{HELPER} status {change_id}", check=False)
                                for vm in tb.vms.values()), 90, 1,
                        )
                        require(armed and process.poll() is None,
                                f"{change_id}: not all nodes armed before applying")
                        for vm in tb.vms.values():
                            require(vm.sh(f"test -s /root/libremesh-failsafe/cfg-{change_id}/changes.uci"
                                          " && echo PASS", check=False).endswith("PASS"),
                                    f"{vm.name}: staged file missing")
                        t.log(f"PASS: {change_id} staged and armed on all nodes")
                        if bad_node:
                            def partitioned() -> bool:
                                a_config, a_link = vlan_status(tb.vms["a"])
                                b_config, b_link = vlan_status(tb.vms["b"])
                                return (a_config == "37" and "id 37" in a_link
                                        and b_config == "38" and "id 38" in b_link
                                        and not ping(tb.vms["a"], "10.77.0.2")
                                        and not ping(tb.vms["b"], "10.77.0.1"))

                            require(scenarios.wait_for(partitioned, 90, 2),
                                    "did not observe the VLAN mismatch and peer isolation")
                            t.log("PASS: mismatched live VLANs isolated node b in both directions")
                        else:
                            time.sleep(50)
                            for name, vm in tb.vms.items():
                                details = vm.sh(
                                    f"{HELPER} status {change_id}; "
                                    f"uci -q get {VLAN}; "
                                    "ip -d link show eth0_29 | grep 'vlan protocol' || true; "
                                    "ip -4 -o addr show dev br-lan; "
                                    f"{HELPER} health "
                                    + " ".join(f"10.77.0.{i}" for i, peer in enumerate("abc", 1)
                                               if peer != name),
                                    timeout=40, check=False,
                                )
                                t.log(f"{change_id} {name} diagnostics:\n{details}")
                        try:
                            code = process.wait(timeout=300)
                        except subprocess.TimeoutExpired as error:
                            raise t.TestbedError(f"{change_id}: controller timed out") from error
                    finally:
                        if process.poll() is None:
                            process.terminate()
                            process.wait(timeout=10)
                    log.seek(0)
                    print(log.read(), flush=True)
                require(code == (1 if bad_node else 0),
                        f"{change_id}: unexpected controller exit {code}")
                wanted_state = "rolledback" if bad_node else "confirmed"
                wanted_vlan = "29" if bad_node else "37"
                for vm in tb.vms.values():
                    require(scenarios.wait_for(
                        lambda: f"state={wanted_state}" in vm.sh(
                            f"{HELPER} status {change_id}", check=False), 90, 2),
                        f"{vm.name}: did not reach {wanted_state}")
                    require(scenarios.wait_for(
                        lambda: vlan_status(vm)[0] == wanted_vlan
                        and f"id {wanted_vlan}" in vlan_status(vm)[1], 90, 2),
                        f"{vm.name}: live VLAN was not restored to {wanted_vlan}")
                peer_mesh(tb)
                t.log(f"PASS: {change_id} {wanted_state}, live VLAN {wanted_vlan}, all peers reachable")

            if args.only != "migration":
                run_change("qemu-partition", bad_node=True)
            if args.only != "partition":
                run_change("qemu-vlan-migration", bad_node=False)
            return 0
        finally:
            tb.stop()


if __name__ == "__main__":
    sys.exit(main())
