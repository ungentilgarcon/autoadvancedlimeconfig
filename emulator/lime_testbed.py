#!/usr/bin/env python3
"""Unprivileged QEMU testbed for the LibreMesh tooling in this repository.

* Every node is an x86-64 QEMU/KVM VM booted from a LibreMesh image built
  through the Firmware Selector with deploy-libremesh.py (or a stock OpenWrt
  image for plain helper routers such as an "ISP").
* Links between VMs are Ethernet segments switched by a small userspace hub
  (no root, no TAP devices). Links can be cut and restored at runtime.
* Each node also has a QEMU "user" NIC used only for management: an SSH port
  forwarded on 127.0.0.1, so deploy-libremesh.py can be pointed at it with
  LIBREMESH_SSH_CONFIG.

Limits: this is NOT the Aruba hardware. There is no QEMU machine for the
IPQ40xx SoC, so Wi-Fi radios (mac80211_hwsim), port/switch naming and
sysupgrade of the real firmware cannot be reproduced.
"""
from __future__ import annotations

import gzip
import os
import re
import secrets
import select
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMAGES = ROOT / "images"
STATE = ROOT / "state"
MGMT_IP = "10.0.2.15"
MGMT_CIDR = f"{MGMT_IP}/24"
MGMT_NETWORK = "10.0.2.0/24"


class TestbedError(Exception):
    pass


def log(message: str) -> None:
    print(f"[testbed {time.strftime('%H:%M:%S')}] {message}", flush=True)


class Hub:
    """Floods Ethernet frames between the QEMU datagram sockets of a link."""

    def __init__(self, rundir: Path) -> None:
        self.rundir = rundir
        self.links: dict[str, dict] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def add_link(self, name: str) -> Path:
        path = self.rundir / f"link-{name}.sock"
        path.unlink(missing_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(str(path))
        sock.setblocking(False)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 22)
        self.links[name] = {"sock": sock, "path": path, "ports": [], "up": True}
        return path

    def add_port(self, link: str, port_path: Path) -> None:
        self.links[link]["ports"].append(str(port_path))

    def set_link(self, name: str, up: bool) -> None:
        self.links[name]["up"] = up
        log(f"link {name} {'restored' if up else 'CUT'}")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        for link in self.links.values():
            link["sock"].close()
            Path(link["path"]).unlink(missing_ok=True)

    def _run(self) -> None:
        by_sock = {link["sock"]: link for link in self.links.values()}
        while not self._stop.is_set():
            readable, _, _ = select.select(list(by_sock), [], [], 0.2)
            for sock in readable:
                link = by_sock[sock]
                try:
                    while True:
                        frame, sender = sock.recvfrom(65536)
                        if not link["up"]:
                            continue
                        for port in link["ports"]:
                            if port == sender:
                                continue
                            try:
                                sock.sendto(frame, port)
                            except OSError:
                                pass
                except BlockingIOError:
                    pass
                except OSError:
                    pass


class Serial:
    """Minimal driver for the OpenWrt serial console (no pexpect needed)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.sock: socket.socket | None = None
        self.buffer = ""
        self.lock = threading.Lock()

    def connect(self, timeout: float = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(str(self.path))
                sock.setblocking(False)
                self.sock = sock
                return
            except OSError:
                time.sleep(0.3)
        raise TestbedError(f"serial socket {self.path} never appeared")

    def _drain(self) -> None:
        assert self.sock
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self.buffer += chunk.decode("utf-8", "replace")
        except BlockingIOError:
            pass

    def write(self, text: str) -> None:
        assert self.sock
        data = text.encode()
        while data:
            _, writable, _ = select.select([], [self.sock], [], 5)
            if not writable:
                raise TestbedError("serial write stalled")
            sent = self.sock.send(data[:512])
            data = data[sent:]
            time.sleep(0.01)

    def run(self, command: str, timeout: float = 60) -> tuple[int, str]:
        """Run a shell command; returns (exit status, combined output)."""
        with self.lock:
            tag = secrets.token_hex(4)
            self._drain()
            self.buffer = ""
            self.write(
                f'echo "@@""B{tag}"; {{ {command}\n}} 2>&1; echo "@@""E{tag}:$?"\n'
            )
            pattern = re.compile(rf"@@B{tag}\r?\n(.*?)@@E{tag}:(\d+)", re.S)
            deadline = time.time() + timeout
            while time.time() < deadline:
                self._drain()
                match = pattern.search(self.buffer)
                if match:
                    return int(match.group(2)), match.group(1).replace("\r", "")
                time.sleep(0.1)
            raise TestbedError(
                f"serial command timed out after {timeout}s: {command[:80]!r}"
            )

    def wait_shell(self, timeout: float = 240) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.write("\n")
                time.sleep(1.5)
                self._drain()
                status, _ = self.run("echo up", timeout=4)
                if status == 0:
                    self.run("stty -echo; dmesg -n 1; true", timeout=10)
                    return
            except TestbedError:
                continue
        raise TestbedError("console never became usable")

    def put_file(self, remote_path: str, content: bytes, mode: str = "644") -> None:
        quoted_path = shlex.quote(remote_path)
        status, output = self.run(f": > {quoted_path}", timeout=15)
        if status:
            raise TestbedError(f"could not create {remote_path}: {output}")
        for index in range(0, len(content), 96):
            chunk = "".join(f"\\{byte:03o}" for byte in content[index:index + 96])
            status, output = self.run(
                f"printf '{chunk}' >> {quoted_path}", timeout=15
            )
            if status:
                raise TestbedError(f"could not write {remote_path}: {output}")
        status, output = self.run(f"chmod {mode} {quoted_path}", timeout=15)
        if status:
            raise TestbedError(f"could not chmod {remote_path}: {output}")


class VM:
    def __init__(
        self,
        name: str,
        index: int,
        base_image: Path,
        links: list[str],
        hub: Hub,
        rundir: Path,
        memory: int = 320,
        ssh_port: int | None = None,
    ) -> None:
        self.name = name
        self.index = index
        self.base_image = base_image
        self.links = links
        self.hub = hub
        self.rundir = rundir
        self.memory = memory
        self.ssh_port = ssh_port
        self.allow_reboot = False
        self._mgmt_nic: str | None = None
        self.process: subprocess.Popen | None = None
        self.serial_path = rundir / f"{name}.serial"
        self.monitor_path = rundir / f"{name}.monitor"
        self.disk = rundir / f"{name}.qcow2"
        self.serial: Serial | None = None
        self.log_file = rundir / f"{name}.qemu.log"

    # Linux interface numbering can vary, so wait_ready resolves this by MAC.
    @property
    def mgmt_nic(self) -> str | None:
        return self._mgmt_nic if self.ssh_port else None

    def start(self) -> None:
        subprocess.run(
            ["qemu-img", "create", "-q", "-f", "qcow2", "-F", "raw",
             "-b", str(self.base_image), str(self.disk), "1G"],
            check=True,
        )
        args = [
            "qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-M", "pc",
            "-m", str(self.memory), "-smp", "1", "-display", "none",
            "-drive", f"file={self.disk},if=virtio,format=qcow2",
            "-serial", f"unix:{self.serial_path},server=on,wait=off",
            "-monitor", f"unix:{self.monitor_path},server=on,wait=off",
        ]
        if not self.allow_reboot:
            args.append("-no-reboot")
        for nic, link in enumerate(self.links):
            port_path = self.rundir / f"{self.name}-eth{nic}.sock"
            port_path.unlink(missing_ok=True)
            self.hub.add_port(link, port_path)
            args += [
                "-netdev",
                f"dgram,id=n{nic},local.type=unix,local.path={port_path},"
                f"remote.type=unix,remote.path={self.hub.links[link]['path']}",
                "-device",
                f"virtio-net-pci,netdev=n{nic},id=nic{nic},"
                f"mac=52:54:00:{self.index:02x}:{nic:02x}:01",
            ]
        if self.ssh_port:
            args += [
                "-netdev",
                f"user,id=mgmt,net={MGMT_NETWORK},"
                f"hostfwd=tcp:127.0.0.1:{self.ssh_port}-{MGMT_IP}:22",
                "-device",
                f"virtio-net-pci,netdev=mgmt,mac=52:54:00:{self.index:02x}:ff:01",
            ]
        log(f"starting {self.name}")
        with self.log_file.open("w") as output:
            self.process = subprocess.Popen(args, stdout=output, stderr=output)
        self.serial = Serial(self.serial_path)

    def wait_ready(self) -> None:
        assert self.serial
        self.serial.connect()
        time.sleep(5)
        self.monitor("sendkey ret")
        time.sleep(10)
        self.serial.wait_shell(timeout=90)
        if self.ssh_port:
            mac = f"52:54:00:{self.index:02x}:ff:01"
            status, output = self.serial.run(
                "ip -o link | awk -v mac='" + mac + "' "
                "'index(tolower($0), tolower(mac)) { split($2, parts, \"@\"); "
                "sub(/:$/, \"\", parts[1]); print parts[1]; exit }'",
                timeout=10,
            )
            if status or not output.strip():
                raise TestbedError(
                    f"could not locate management NIC {mac} on {self.name}"
                )
            self._mgmt_nic = output.strip().splitlines()[0]
        if self.mgmt_nic:
            self.sh(
                f"batctl meshif bat0 interface del {self.mgmt_nic} 2>/dev/null || true; "
                f"ip link set {self.mgmt_nic} nomaster 2>/dev/null || true; "
                f"ip addr add {MGMT_CIDR} dev {self.mgmt_nic}; "
                f"ip link set {self.mgmt_nic} up"
            )

    def sh(self, command: str, timeout: float = 60, check: bool = True) -> str:
        assert self.serial
        status, output = self.serial.run(command, timeout)
        if check and status != 0:
            raise TestbedError(f"{self.name}: `{command[:100]}` exited {status}:\n{output}")
        return output.strip()

    def monitor(self, command: str) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(self.monitor_path))
            sock.settimeout(3)
            try:
                sock.recv(4096)
            except socket.timeout:
                pass
            sock.sendall(command.encode() + b"\n")
            time.sleep(0.3)

    def set_nic_link(self, nic: int, up: bool) -> None:
        self.monitor(f"set_link nic{nic} {'on' if up else 'off'}")
        log(f"{self.name} eth{nic} cable {'plugged' if up else 'unplugged'}")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


class Testbed:
    def __init__(self, name: str = "lab") -> None:
        self.name = name
        self.rundir = Path(f"/tmp/lime-testbed-{os.getuid()}-{name}")
        if self.rundir.exists():
            shutil.rmtree(self.rundir)
        self.rundir.mkdir(mode=0o700, parents=True)
        STATE.mkdir(exist_ok=True, mode=0o700)
        self.hub = Hub(self.rundir)
        self.vms: dict[str, VM] = {}
        self.ssh_config = STATE / "ssh_config"
        self.key = STATE / "id_ed25519"
        self.known_hosts = STATE / "known_hosts"
        self._next_ssh_port = 22210

    def link(self, name: str) -> None:
        if name not in self.hub.links:
            self.hub.add_link(name)

    def add_vm(self, name: str, base_image: Path, links: list[str],
               management: bool = True, memory: int = 320) -> VM:
        for link in links:
            self.link(link)
        port = None
        if management:
            port = self._next_ssh_port
            self._next_ssh_port += 1
        vm = VM(name, len(self.vms) + 1, base_image, links, self.hub,
                self.rundir, memory, port)
        self.vms[name] = vm
        return vm

    def start(self) -> None:
        self.hub.start()
        for vm in self.vms.values():
            vm.start()
        errors: list[Exception] = []

        def guarded(vm: VM) -> None:
            try:
                vm.wait_ready()
            except TestbedError as error:
                errors.append(TestbedError(f"{vm.name}: {error}"))

        threads = [threading.Thread(target=guarded, args=(vm,)) for vm in self.vms.values()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            raise TestbedError(f"boot failed: {errors[0]}") from errors[0]

    def stop(self) -> None:
        for vm in self.vms.values():
            vm.stop()
        self.hub.stop()
        shutil.rmtree(self.rundir, ignore_errors=True)

    # -- SSH access for deploy-libremesh.py --------------------------------

    def setup_ssh(self, names: list[str] | None = None) -> None:
        if not self.key.exists():
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "lime-testbed",
                 "-f", str(self.key)],
                check=True,
            )
        public_key = Path(f"{self.key}.pub").read_text().strip()
        self.known_hosts.write_text("")
        entries = []
        for vm in self.vms.values():
            if names is not None and vm.name not in names:
                continue
            if not vm.ssh_port or not vm.serial:
                continue
            vm.sh("mkdir -p /etc/dropbear && chmod 700 /etc/dropbear")
            vm.serial.put_file("/etc/dropbear/authorized_keys", public_key.encode() + b"\n", "600")
            vm.sh(
                "/etc/init.d/firewall stop >/dev/null 2>&1 || true; "
                "/etc/init.d/dropbear restart; true"
            )
            deadline = time.time() + 30
            scan = None
            while time.time() < deadline:
                scan = subprocess.run(
                    ["ssh-keyscan", "-T", "2", "-p", str(vm.ssh_port), "127.0.0.1"],
                    capture_output=True, text=True, check=False,
                )
                if scan.stdout.strip():
                    break
                time.sleep(1)
            if not scan.stdout.strip():
                state = vm.sh(
                    "ip -br addr; pidof dropbear; ls -l /etc/dropbear; "
                    "netstat -lnt 2>/dev/null | grep ':22 ' || true",
                    check=False,
                )
                raise TestbedError(
                    f"could not read SSH host key of {vm.name}: "
                    f"{scan.stderr.strip()} {state}"
                )
            with self.known_hosts.open("a") as handle:
                handle.write(scan.stdout)
            entries.append(
                f"Host {vm.name}\n"
                f"    HostName 127.0.0.1\n    Port {vm.ssh_port}\n    User root\n"
                f"    IdentityFile {self.key}\n    IdentitiesOnly yes\n"
                f"    UserKnownHostsFile {self.known_hosts}\n"
            )
        self.ssh_config.write_text("\n".join(entries))
        os.chmod(self.ssh_config, 0o600)

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["LIBREMESH_SSH_CONFIG"] = str(self.ssh_config)
        return env


def decompress(source: Path, target: Path) -> Path:
    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    with gzip.open(source, "rb") as src, target.open("wb") as dst:
        try:
            shutil.copyfileobj(src, dst)
        except (EOFError, OSError) as error:
            if "trailing" not in str(error).lower() and target.stat().st_size < 1 << 20:
                raise
    return target


if __name__ == "__main__":
    print(__doc__)
    sys.exit(0)
