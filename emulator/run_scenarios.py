#!/usr/bin/env python3
"""Run the repository's tools against a 4-VM virtual network.

Topology (triangle of LibreMesh nodes plus one plain-OpenWrt "ISP"):

    A --ab-- B --bc-- C --ac-- A          wired mesh links (eth0/eth2 ...)
    A.eth1 (stock "wan" port) --isp-- ISP
    C.eth3 (a normal LAN port, cable unplugged at start) --isp-- ISP

Usage: run_scenarios.py [--only mesh,failover,vlan,vlan-rollback,autowan]
"""
from __future__ import annotations

import argparse
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import lime_testbed as t

REPO = t.ROOT.parent
BOARD = "qemu-standard-pc-i440fx-piix-1996"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    t.log(f"{'PASS' if ok else 'FAIL'}: {name} {detail if not ok else ''}")
    return ok


def wait_for(fn, timeout: float, interval: float = 5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if fn():
                return True
        except t.TestbedError:
            pass
        time.sleep(interval)
    return False


def tool(tb: t.Testbed, *args: str, timeout: int = 400) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "deploy-libremesh.py"), *args],
        env=tb.env(), capture_output=True, text=True, timeout=timeout, check=False,
    )


def mesh_ip(vm: t.VM) -> str:
    return vm.sh("ip -4 -o addr show dev bat0 | awk '{print $4}' | head -1 | cut -d/ -f1")


def build_topology(
    image: Path, stock: Path, with_isp: bool = True, autowan_only: bool = False
) -> t.Testbed:
    tb = t.Testbed("lab")
    if with_isp:
        tb.add_vm("isp", stock, ["isp"], management=False, memory=256)
        if autowan_only:
            tb.add_vm("c", image, ["ac", "wanc", "isp"], memory=448)
        else:
            tb.add_vm("a", image, ["ab", "isp", "ac"], memory=448)
            tb.add_vm("b", image, ["ab", "wanb", "bc"], memory=448)
            tb.add_vm("c", image, ["ac", "wanc", "bc", "isp"], memory=448)
    else:
        tb.add_vm("a", image, ["ab", "wana", "ac"], memory=448)
        tb.add_vm("b", image, ["ab", "wanb", "bc"], memory=448)
        tb.add_vm("c", image, ["ac", "wanc", "bc"], memory=448)
    return tb


def prepare(tb: t.Testbed, configure_ssh: bool = True) -> None:
    if "isp" in tb.vms:
        isp = tb.vms["isp"]
        isp.sh(
            "/etc/init.d/firewall stop; "
            "ip addr add 1.1.1.1/32 dev lo; ip addr add 9.9.9.9/32 dev lo; "
            "ip addr add 8.8.8.8/32 dev lo; true"
        )
        if "b" in tb.vms:
            tb.vms["b"].set_nic_link(1, False)
        if "c" in tb.vms:
            tb.vms["c"].set_nic_link(1, False)
            tb.vms["c"].set_nic_link(len(tb.vms["c"].links) - 1, False)
    else:
        for name in "abc":
            tb.vms[name].set_nic_link(1, False)
    for vm in tb.vms.values():
        if vm.name == "isp" or not vm.mgmt_nic:
            continue
        vm.sh(
            "uci -q delete network.testmgmt; uci set network.testmgmt=interface; "
            f"uci set network.testmgmt.device={vm.mgmt_nic}; "
            "uci set network.testmgmt.proto=static; "
            "uci set network.testmgmt.ipaddr=10.0.2.15/24; uci commit network; true"
        )
    for index, name in enumerate((n for n in "abc" if n in tb.vms), start=1):
        tb.vms[name].sh(f"ip addr add 10.77.0.{index}/24 dev bat0")
    if configure_ssh:
        tb.setup_ssh([n for n in tb.vms if n != "isp"])


def scenario_mesh(tb: t.Testbed) -> None:
    ips = {n: mesh_ip(tb.vms[n]) for n in "abc"}
    t.log(f"mesh addresses: {ips}")
    for src, dst in (("a", "b"), ("b", "c"), ("c", "a")):
        ok = wait_for(
            lambda s=src, d=dst: tb.vms[s].sh(f"ping -c2 -W2 {ips[d]} >/dev/null", check=False) == "",
            180,
        )
        check(f"mesh ping {src}->{dst}", ok)
    neigh = tb.vms["a"].sh("batctl meshif bat0 n 2>&1 | head -6", check=False)
    check("batman-adv has neighbours on A", "eth" in neigh, neigh)
    babel = tb.vms["a"].sh("pidof babeld; true", check=False)
    check("babeld daemon running on A", bool(babel), babel)


def scenario_failover(tb: t.Testbed) -> None:
    ips = {n: mesh_ip(tb.vms[n]) for n in "abc"}
    tb.hub.set_link("ac", False)
    ok = wait_for(
        lambda: tb.vms["a"].sh(f"ping -c2 -W2 {ips['c']} >/dev/null", check=False) == "",
        150,
    )
    check("A reaches C through B after cutting A-C", ok)
    tb.hub.set_link("ac", True)


def write_vlan_config(role: str, directory: Path) -> Path:
    path = directory / f"{role}.conf"
    path.write_text(
        "config vlan 'settings'\n\toption enabled '1'\n"
        f"\toption role '{role}'\n\toption radios 'radio0 radio1'\n"
        "\toption iot_ssid 'testbed-IOT'\n\toption guest_ssid 'testbed-GUEST'\n"
    )
    return path


def scenario_vlan(tb: t.Testbed) -> None:
    with tempfile.TemporaryDirectory(dir=t.STATE) as directory:
        directory = Path(directory)
        passfile = directory / "pass"
        passfile.write_text(
            f"IOT_WIFI_KEY={secrets.token_urlsafe(24)}\n"
            f"GUEST_WIFI_KEY={secrets.token_urlsafe(24)}\n"
        )
        passfile.chmod(0o600)
        gateway = write_vlan_config("gateway", directory)
        edge = write_vlan_config("edge", directory)
        common = ["--device", BOARD, "--pass-file", str(passfile)]
        for name, cfg in (("a", gateway), ("b", edge)):
            result = tool(tb, "vlans", *common, "--host", name, "--config", str(cfg))
            check(f"vlans apply on {name}", result.returncode == 0,
                  (result.stdout + result.stderr)[-800:])
            if result.returncode == 0:
                match = re.search(r"VLAN_CHANGE_ID=([0-9-]+)", result.stdout)
                if not match:
                    check(f"vlan-confirm on {name}", False, "apply output omitted change ID")
                else:
                    confirm = tool(
                        tb, "vlan-confirm", "--device", BOARD, "--host", name,
                        "--change-id", match.group(1),
                    )
                    check(f"vlan-confirm on {name}", confirm.returncode == 0,
                          (confirm.stdout + confirm.stderr)[-500:])
        a, b, c = (tb.vms[n] for n in "abc")
        check("bat0.10 and bat0.20 exist on gateway",
              a.sh("ip link show bat0.10 >/dev/null && ip link show bat0.20 >/dev/null; echo $?", check=False).endswith("0"))
        a.sh(
            "uci set dhcp.@dnsmasq[0].logdhcp=1; uci commit dhcp; "
            "/etc/init.d/dnsmasq restart"
        )
        lease = b.sh(
            "udhcpc -i bat0.10 -n -q -t 3 -T 2 -s /bin/true 2>&1 || true",
            timeout=15,
            check=False,
        )
        if "lease of 192.168.10." not in lease:
            lease += "\ngateway: " + a.sh(
                "ip -d link show bat0.10; ip -4 addr show dev bat0.10; "
                "uci show dhcp.picot_vlan_iot_dhcp; uci show network.picot_vlan_iot; "
                "grep -hE 'interface=|dhcp-range=' /var/etc/dnsmasq.conf.* 2>/dev/null; "
                "nft list chain inet fw4 input 2>&1; "
                "logread | grep -E 'dnsmasq|DHCP' | tail -10; "
                "batctl meshif bat0 vid 10 vlan_json 2>&1; "
                "batctl meshif bat0 n | head -5",
                check=False,
            )
            lease += "\nedge: " + b.sh(
                "ip -d link show bat0.10; ip -4 addr show dev bat0.10; "
                "ip neigh show dev bat0.10; "
                "batctl meshif bat0 vid 10 vlan_json 2>&1; "
                "batctl meshif bat0 n | head -5",
                check=False,
            )
        check("edge node gets an IoT (VLAN 10) lease from the gateway over bat0.10",
              "lease of 192.168.10." in lease, lease[-2500:])
        a.sh(
            "nft insert rule inet fw4 input iifname 'bat0.10' "
            "ip protocol icmp accept"
        )
        vlan_ping = b.sh(
            "ip addr add 192.168.10.2/24 dev bat0.10; "
            "ping -c2 -W2 192.168.10.1 2>&1; result=$?; "
            "ip neigh show dev bat0.10; "
            "ip addr del 192.168.10.2/24 dev bat0.10; echo PING_STATUS=$result",
            timeout=30,
            check=False,
        )
        check("edge reaches gateway across bat0.10 with static test address",
              "PING_STATUS=0" in vlan_ping, vlan_ping[-500:])
        ips = {n: mesh_ip(tb.vms[n]) for n in "abc"}
        check("non-VLAN node C still reaches VLAN gateway A",
              wait_for(lambda: c.sh(f"ping -c2 -W2 {ips['a']} >/dev/null", check=False) == "", 60))
        check("non-VLAN node C still reaches VLAN edge B",
              wait_for(lambda: c.sh(f"ping -c2 -W2 {ips['b']} >/dev/null", check=False) == "", 60))
        result = tool(tb, "vlans", *common, "--host", "b", "--disable")
        check("vlans --disable on edge B", result.returncode == 0,
              (result.stdout + result.stderr)[-800:])
        if result.returncode == 0:
            match = re.search(r"VLAN_CHANGE_ID=([0-9-]+)", result.stdout)
            if match:
                tool(tb, "vlan-confirm", "--device", BOARD, "--host", "b",
                     "--change-id", match.group(1))
        gone = b.sh("uci -q show network | grep -c picot_vlan; true", check=False).strip()
        check("VLAN sections removed on B", gone.endswith("0"), gone)
        check("B still meshes with VLAN gateway A after --disable",
              wait_for(lambda: b.sh(f"ping -c2 -W2 {ips['a']} >/dev/null", check=False) == "", 60))


def scenario_vlan_rollback(tb: t.Testbed) -> None:
    with tempfile.TemporaryDirectory(dir=t.STATE) as directory:
        directory = Path(directory)
        passfile = directory / "pass"
        passfile.write_text(
            f"IOT_WIFI_KEY={secrets.token_urlsafe(24)}\n"
            f"GUEST_WIFI_KEY={secrets.token_urlsafe(24)}\n"
        )
        passfile.chmod(0o600)
        edge = write_vlan_config("edge", directory)
        c = tb.vms["c"]
        result = tool(tb, "vlans", "--device", BOARD, "--pass-file", str(passfile),
                      "--host", "c", "--config", str(edge))
        check("vlans apply on C (left unconfirmed)", result.returncode == 0,
              (result.stdout + result.stderr)[-800:])
        check("VLAN config active before timeout",
              c.sh("uci -q show network | grep -c picot_vlan; true", check=False).strip() != "0")
        match = re.search(r"VLAN_CHANGE_ID=([0-9-]+)", result.stdout)
        if match:
            rollback = tool(
                tb, "vlan-rollback", "--device", BOARD, "--host", "c",
                "--change-id", match.group(1),
            )
            check("explicit vlan-rollback on C", rollback.returncode == 0,
                  (rollback.stdout + rollback.stderr)[-500:])
        else:
            check("explicit vlan-rollback on C", False, "apply output omitted change ID")
        after = c.sh("uci -q show network | grep -c picot_vlan; true", check=False).strip()
        check("VLAN sections removed after rollback", after.endswith("0"), after)


def scenario_autowan(tb: t.Testbed) -> None:
    c = tb.vms["c"]
    port_index = len(c.links) - 1
    port = f"eth{port_index}"
    c.sh(
        "printf \"config autowan 'settings'\\n\" > /etc/config/picot-autowan; "
        "uci set picot-autowan.settings=autowan; "
        f"uci set picot-autowan.settings.exclude_ports={c.mgmt_nic}; "
        "uci set picot-autowan.settings.down_seconds=2; "
        "uci set picot-autowan.settings.ping_hosts=1.1.1.1; "
        "uci commit picot-autowan"
    )
    assert c.serial
    c.serial.put_file("/usr/sbin/picot-autowan", (REPO / "picot-autowan").read_bytes(), "755")
    c.serial.put_file("/etc/init.d/picot-autowan", (REPO / "picot-autowan.init").read_bytes(), "755")
    install = c.sh(
        "/usr/sbin/picot-autowan check && "
        "/usr/sbin/picot-autowan configure enable && "
        "/etc/init.d/picot-autowan enable && /etc/init.d/picot-autowan restart; echo $?",
        check=False,
    )
    check("autowan install and enable on C", install.endswith("0"), install[-800:])
    t.log(c.sh(
        "/usr/sbin/picot-autowan status; ps w | grep '[p]icot-autowan'; true",
        check=False,
    ))
    c.set_nic_link(port_index, True)
    promoted = wait_for(
        lambda: c.sh("uci -q get network.wan.device; true", check=False).strip() == port,
        90, 5,
    )
    state = c.sh("/usr/sbin/picot-autowan status; true", check=False)
    state += "\nLOG: " + c.sh("logread | grep -i picot-autowan | tail -10; true", check=False)
    state += "\nPORT: " + c.sh(f"ip link show {port}; uci show network.wan; true", check=False)
    state += "\nPROCESS: " + c.sh("ps w | grep '[p]icot-autowan'; true", check=False)
    if "isp" in tb.vms:
        state += "\nISP: " + tb.vms["isp"].sh(
            "ip addr show br-lan; pidof dnsmasq; true",
            check=False,
        )
    check(f"LAN port {port} promoted to wan on C after cabling to the ISP", promoted,
          state[-2500:])
    if promoted:
        ping = ""

        def ping_isp() -> bool:
            nonlocal ping
            ping = c.sh(f"ping -c1 -W2 -I {port} 1.1.1.1 2>&1; true", check=False)
            return bool(re.search(r"1 (?:packets )?received", ping))

        got = wait_for(ping_isp, 30, 2)
        detail = ping
        if not got:
            detail += "\nRoutes: " + c.sh(
                "ip route; ip addr show dev " + port + "; true", check=False
            )
            detail += "\nISP: " + tb.vms["isp"].sh(
                "ip addr show lo; true", check=False
            )
        check("C reaches the 'Internet' (1.1.1.1) through the promoted port", got, detail)
        c.set_nic_link(port_index, False)
        demoted = wait_for(
            lambda: c.sh("uci -q get network.wan.device; true", check=False).strip() != port,
            45, 5,
        )
        check("port demoted after cable removal", demoted,
              c.sh("logread | grep -i autowan | tail -8; true", check=False))
    if "b" in tb.vms:
        default = tb.vms["b"].sh("ip route | grep '^default'; true", check=False)
        t.log(f"B default routes: {default}")


SCENARIOS = {
    "mesh": scenario_mesh,
    "failover": scenario_failover,
    "vlan": scenario_vlan,
    "autowan": scenario_autowan,
    "vlan-rollback": scenario_vlan_rollback,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=",".join(SCENARIOS))
    parser.add_argument("--image", type=Path, default=t.IMAGES / "lime.img")
    args = parser.parse_args()
    stock = t.IMAGES / "stock.img"
    selected = args.only.split(",")
    autowan_only = selected == ["autowan"]
    tb = build_topology(
        args.image, stock, with_isp="autowan" in selected, autowan_only=autowan_only
    )
    try:
        tb.start()
        prepare(tb, configure_ssh=any(
            name in {"vlan", "vlan-rollback"} for name in selected
        ))
        for name in selected:
            t.log(f"=== scenario {name} ===")
            try:
                SCENARIOS[name](tb)
            except Exception as error:  # noqa: BLE001
                check(f"scenario {name} ran without error", False, repr(error)[:600])
    finally:
        tb.stop()
    print("\n==== SUMMARY ====")
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok and detail:
            print("      " + detail.strip().replace("\n", "\n      ")[:700])
    return 0 if all(ok for _, ok, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
