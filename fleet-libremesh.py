#!/usr/bin/env python3
"""Fleet upgrades for a mixed-hardware LibreMesh network.

Change types:
  firmware         sysupgrade with config kept, canary + batches, gateway last
  config-safe      non-breaking config, applied node by node with rollback
  config-breaking  connectivity-affecting config: staged everywhere first, then
                   applied simultaneously, confirmed only if the whole mesh is
                   healthy, otherwise rolled back (also by on-node watchdogs)
Also: status, backup, rollback.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("deploy_libremesh", ROOT / "deploy-libremesh.py")
dl = importlib.util.module_from_spec(_spec)
sys.modules["deploy_libremesh"] = dl
_spec.loader.exec_module(dl)

DeployError = dl.DeployError
HELPER = "/usr/sbin/picot-config-sync"
RECOVERY = "/etc/init.d/picot-config-recovery"
HOST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@:-]*$")
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
UCI_VERBS = ("set", "add", "add_list", "del_list", "delete", "rename", "reorder")

# Packages a config-safe change may touch. Anything that can cut mesh
# connectivity (network, wireless, firewall, dropbear, babeld, ...) is excluded.
SAFE_PACKAGES = {"system", "uhttpd", "luci", "rpcd", "picot-autowan", "watchping", "eupgrade"}
LIME_RISKY = re.compile(
    r"(mesh|channel|htmode|country|bssid|encryption|key|network|address|"
    r"vlan|proto|distance|ap_ssid|apname|adhoc|batadv|babel|bmx)", re.I)


# ---------------------------------------------------------------- inventory

class Node:
    def __init__(self, raw: dict):
        self.name = raw["name"]
        self.host = raw["host"]
        self.device = raw.get("device")
        self.role = raw.get("role", "node")
        self.mesh_ip = raw.get("mesh_ip")
        self.canary = bool(raw.get("canary", False))
        self.offset = 0.0

    def __str__(self) -> str:
        return self.name


def load_inventory(path: Path) -> tuple[list[Node], dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DeployError(f"Cannot read inventory {path}: {error}") from error
    nodes = []
    seen = set()
    for raw in data.get("nodes", []):
        for key in ("name", "host"):
            if not isinstance(raw.get(key), str) or not raw[key]:
                raise DeployError(f"Inventory node is missing {key!r}: {raw}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", raw["name"]) or raw["name"] in seen:
            raise DeployError(f"Invalid or duplicate node name: {raw['name']!r}")
        if not HOST_RE.fullmatch(raw["host"]):
            raise DeployError(f"Invalid host for {raw['name']}: {raw['host']!r}")
        if raw.get("role", "node") not in ("node", "gateway"):
            raise DeployError(f"role must be node or gateway: {raw['name']}")
        ip = raw.get("mesh_ip")
        if ip is not None and not re.fullmatch(r"[0-9a-fA-F:.]{2,45}", ip):
            raise DeployError(f"Invalid mesh_ip for {raw['name']}")
        seen.add(raw["name"])
        nodes.append(Node(raw))
    if not nodes:
        raise DeployError("Inventory has no nodes")
    return nodes, data


def select_nodes(nodes: list[Node], only: list[str] | None) -> list[Node]:
    if not only:
        return nodes
    unknown = set(only) - {n.name for n in nodes}
    if unknown:
        raise DeployError(f"Unknown node(s): {', '.join(sorted(unknown))}")
    return [n for n in nodes if n.name in only]


def rollout_order(nodes: list[Node]) -> list[list[Node]]:
    """Canaries first, then ordinary nodes, gateways last."""
    canary = [n for n in nodes if n.canary]
    rest = [n for n in nodes if not n.canary and n.role != "gateway"]
    gateways = [n for n in nodes if not n.canary and n.role == "gateway"]
    return [g for g in (canary, rest, gateways) if g]


# ---------------------------------------------------------------- transport

PASS_VALUES: dict[str, str] = {}


def load_passwords(path: Path) -> None:
    """Read optional SSH passwords from the private .pass file (never printed)."""
    PASS_VALUES.clear()
    if path.is_file():
        PASS_VALUES.update(dl.read_equipment_pass(path, require_vlan_keys=False))


def node_password(node: Node) -> str | None:
    suffix = re.sub(r"[^A-Z0-9]", "_", node.name.upper())
    return PASS_VALUES.get(f"FLEET_SSH_PASSWORD_{suffix}") or PASS_VALUES.get("FLEET_SSH_PASSWORD") or None


def transport(node: Node, base: list[str]) -> tuple[list[str], dict | None]:
    """Prefix with sshpass when a password is configured; it is passed via the
    environment so it never appears in the process list."""
    command = [*base, "-o", "ConnectTimeout=8"]
    password = node_password(node)
    if not password:
        return command, None
    if not shutil.which("sshpass"):
        raise DeployError("FLEET_SSH_PASSWORD is set but sshpass is not installed")
    command = ["sshpass", "-e", *("BatchMode=no" if a == "BatchMode=yes" else a for a in command)]
    return command, {**os.environ, "SSHPASS": password}


def remote(node: Node, command: str, timeout: int = 60) -> subprocess.CompletedProcess:
    argv, env = transport(node, dl.SSH_CMD)
    try:
        return subprocess.run([*argv, node.host, command], capture_output=True, text=True,
                              timeout=timeout, check=False, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([], 255, "", "timeout")


def push(node: Node, local: Path, remote_path: str) -> bool:
    argv, env = transport(node, dl.SCP_CMD)
    try:
        result = subprocess.run([*argv, str(local), f"{node.host}:{remote_path}"],
                                capture_output=True, text=True, timeout=600, check=False, env=env)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def kv(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def parallel(nodes: list[Node], fn) -> dict[str, object]:
    if not nodes:
        return {}
    with ThreadPoolExecutor(max_workers=min(16, len(nodes))) as pool:
        return dict(zip((n.name for n in nodes), pool.map(fn, nodes)))


def peers_for(node: Node, nodes: list[Node]) -> str:
    return " ".join(n.mesh_ip for n in nodes if n is not node and n.mesh_ip)


def node_health(node: Node, nodes: list[Node]) -> dict[str, str] | None:
    result = remote(node, f"{HELPER} health {peers_for(node, nodes)}", timeout=40)
    if result.returncode != 0:
        return None
    return kv(result.stdout)


def healthy(info: dict[str, str] | None, nodes_total: int) -> bool:
    if not info or info.get("health") != "ok":
        return False
    return info.get("peers_ok") == info.get("peers_total")


def install_helpers(node: Node) -> None:
    nonce = uuid.uuid4().hex
    commands = []
    for local, dest in ((ROOT / "picot-config-sync", HELPER),
                        (ROOT / "picot-config-recovery", RECOVERY)):
        temporary = f"/tmp/picot-helper-{nonce}-{local.name}"
        if not push(node, local, temporary):
            raise DeployError(f"{node}: could not upload {local.name}")
        commands.append(f"chmod 755 {temporary}; mv {temporary} {dest}")
    result = remote(node, "set -e; mkdir -p /root/libremesh-failsafe; "
                    "command -v flock >/dev/null; exec 9>/root/libremesh-failsafe/operation.lock; "
                    "flock -x 9; test ! -s /root/libremesh-failsafe/cfg-pending; "
                    + "; ".join(commands) + f"; {RECOVERY} enable; {HELPER} status")
    if result.returncode:
        raise DeployError(f"{node}: helper not runnable: {result.stderr.strip()}")


def clock_offset(node: Node) -> float:
    """Remote clock minus local clock, from the lowest-RTT of three samples."""
    best = None
    for _ in range(3):
        t0 = time.time()
        result = remote(node, "date +%s", timeout=15)
        t1 = time.time()
        if result.returncode or not result.stdout.strip().isdigit():
            continue
        rtt = t1 - t0
        sample = (rtt, int(result.stdout.strip()) - (t0 + t1) / 2)
        if best is None or sample[0] < best[0]:
            best = sample
    if best is None:
        raise DeployError(f"{node}: cannot read the node clock")
    return best[1]


# ---------------------------------------------------------------- config

def load_change(path: Path) -> dict:
    try:
        change = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DeployError(f"Cannot read change file {path}: {error}") from error
    if change.get("kind") not in ("config-safe", "config-breaking"):
        raise DeployError("change 'kind' must be config-safe or config-breaking")
    if change.get("apply", "reload") not in ("lime", "reload", "none"):
        raise DeployError("change 'apply' must be lime, reload or none")
    for key in ("uci",):
        if not isinstance(change.get(key, []), list):
            raise DeployError(f"change {key!r} must be a list")
    if not isinstance(change.get("node_uci", {}), dict):
        raise DeployError("change 'node_uci' must map node name to a list")
    return change


def lines_for(change: dict, node: Node) -> list[str]:
    lines = [*change.get("uci", []), *change.get("node_uci", {}).get(node.name, [])]
    for line in lines:
        if not isinstance(line, str) or "\n" in line or not line.strip():
            raise DeployError(f"Invalid uci line: {line!r}")
        if line.split()[0] not in UCI_VERBS:
            raise DeployError(f"Unsupported uci verb in line: {line!r}")
    return lines


def safety_problems(lines: list[str]) -> list[str]:
    problems = []
    for line in lines:
        parts = line.split(None, 1)
        target = parts[1] if len(parts) > 1 else ""
        package = re.split(r"[.=]", target, maxsplit=1)[0]
        if package in SAFE_PACKAGES:
            continue
        if package == "lime" and not LIME_RISKY.search(target):
            continue
        problems.append(line)
    return problems


def stage_everywhere(nodes: list[Node], change_id: str, change: dict, tmp: Path) -> bool:
    def stage(node: Node) -> bool:
        lines = lines_for(change, node)
        if not lines:
            print(f"  {node}: no changes for this node (skipped)")
            return True
        local = tmp / f"{change_id}-{node.name}.uci"
        local.write_text("\n".join(lines) + "\n", encoding="utf-8")
        local.chmod(0o600)
        remote_file = f"/tmp/cfg-{change_id}.uci"
        if not push(node, local, remote_file):
            print(f"  {node}: upload failed")
            return False
        result = remote(node, f"{HELPER} stage {change_id} {remote_file} {change.get('apply', 'reload')}")
        ok = result.returncode == 0
        print(f"  {node}: {'staged' if ok else 'REJECTED ' + result.stderr.strip()}")
        return ok
    return all(parallel(nodes, stage).values())


def affected(nodes: list[Node], change: dict) -> list[Node]:
    return [n for n in nodes if lines_for(change, n)]


def cancel_all(nodes: list[Node], change_id: str) -> None:
    parallel(nodes, lambda n: remote(n, f"{HELPER} cancel {change_id}"))


def cmd_config(args: argparse.Namespace) -> int:
    nodes, _ = load_inventory(args.inventory)
    nodes = select_nodes(nodes, args.node)
    change = load_change(args.change)
    change_id = args.id or time.strftime("%Y%m%d-%H%M%S")
    if not ID_RE.fullmatch(change_id):
        raise DeployError("Invalid change ID")
    kind = change["kind"]
    targets = affected(nodes, change)
    if not targets:
        raise DeployError("The change contains no uci lines for the selected nodes")

    if kind == "config-safe" and not args.no_safety_guard:
        for node in targets:
            bad = safety_problems(lines_for(change, node))
            if bad:
                raise DeployError(
                    f"Not a safe change ({node}): {bad[0]!r}. Use kind config-breaking, "
                    "or --no-safety-guard if you are sure it cannot affect connectivity.")

    print(f"{kind} change {change_id} on {', '.join(map(str, targets))}")
    if args.dry_run:
        for node in targets:
            print(f"--- {node}")
            print("\n".join(lines_for(change, node)))
        return 0

    for node in targets:
        install_helpers(node)
    # Pre-flight: never start a change on an already unhealthy mesh.
    bad = [n.name for n in targets if not healthy(node_health(n, nodes), len(nodes))]
    if bad and not args.force:
        raise DeployError(f"Mesh not healthy before the change: {', '.join(bad)} (use --force)")

    tmp = Path(__import__("tempfile").mkdtemp(prefix="fleet-"))
    try:
        print("Staging on every node...")
        if not stage_everywhere(targets, change_id, change, tmp):
            cancel_all(targets, change_id)
            print("Aborted: at least one node refused the change; nothing was applied.")
            return 1
    finally:
        for f in tmp.glob("*"):
            f.unlink()
        tmp.rmdir()

    if kind == "config-safe":
        return rollout_safe(targets, nodes, change_id, args)
    return rollout_breaking(targets, nodes, change_id, args)


def rollout_safe(targets: list[Node], nodes: list[Node], change_id: str, args) -> int:
    for group in rollout_order(targets):
        for node in group:
            print(f"Applying on {node}...")
            result = remote(node, f"{HELPER} apply-now {change_id} {args.window}", timeout=120)
            info = node_health(node, nodes)
            if result.returncode or not healthy(info, len(nodes)):
                print(f"  {node}: FAILED ({result.stderr.strip() or info}); rolling back")
                remote(node, f"{HELPER} rollback {change_id}", timeout=90)
                cancel_all([n for n in targets if n is not node], change_id)
                return 1
            remote(node, f"{HELPER} confirm {change_id}")
            print(f"  {node}: ok")
    print("Done.")
    return 0


def rollout_breaking(targets: list[Node], nodes: list[Node], change_id: str, args) -> int:
    print("Measuring node clocks...")
    try:
        offsets = parallel(targets, clock_offset)
    except DeployError as error:
        cancel_all(targets, change_id)
        raise error
    for node in targets:
        node.offset = offsets[node.name]
    target = time.time() + args.lead
    print(f"All nodes staged. Arming simultaneous apply in {args.lead}s "
          f"(rollback window {args.window}s).")

    def arm(node: Node) -> bool:
        epoch = int(round(target + node.offset))
        result = remote(node, f"{HELPER} apply-at {change_id} {epoch} {args.window}")
        return result.returncode == 0
    if not all(parallel(targets, arm).values()):
        cancel_all(targets, change_id)
        print("Aborted: could not arm every node; nothing was applied.")
        return 1

    time.sleep(max(0, target - time.time()) + 5)
    settle_deadline = time.time() + max(60, args.window - 150)
    ok = False
    while time.time() < settle_deadline:
        def check(node: Node) -> bool:
            st = remote(node, f"{HELPER} status {change_id}", timeout=30)
            if st.returncode or kv(st.stdout).get("state") != "applied":
                return False
            return healthy(node_health(node, nodes), len(nodes))
        results = parallel(targets, check)
        print("  " + ", ".join(f"{k}:{'ok' if v else '..'}" for k, v in results.items()))
        if all(results.values()):
            ok = True
            break
        time.sleep(10)

    if not ok:
        print("Mesh did not converge healthy: rolling back.")
        parallel(targets, lambda n: remote(n, f"{HELPER} rollback {change_id}", timeout=90))
        print("Unreachable nodes roll themselves back when their window expires "
              f"(within {args.window}s) or on reboot.")
        return 1

    # Lengthen the window first so a slow confirm cannot trigger a partial rollback.
    parallel(targets, lambda n: remote(n, f"{HELPER} extend {change_id} 600"))
    confirmed = {}
    for attempt in range(3):
        pending = [n for n in targets if not confirmed.get(n.name)]
        if not pending:
            break
        confirmed.update(parallel(pending, lambda n: remote(n, f"{HELPER} confirm {change_id}").returncode == 0))
        time.sleep(2)
    if not all(confirmed.get(n.name) for n in targets):
        lost = [n.name for n in targets if not confirmed.get(n.name)]
        print(f"WARNING: could not confirm on {', '.join(lost)}; they will roll back "
              "when the extended window expires while the others keep the change. "
              "Re-run confirm manually or investigate.")
        return 2
    print("Confirmed on all nodes.")
    return 0


# ---------------------------------------------------------------- firmware

def wait_for_reboot(node: Node, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:  # wait for it to go down
        if remote(node, "true", timeout=8).returncode != 0:
            break
        time.sleep(3)
    else:
        return False
    while time.time() < deadline:
        if remote(node, "true", timeout=10).returncode == 0:
            return True
        time.sleep(5)
    return False


def cmd_firmware(args: argparse.Namespace) -> int:
    nodes, data = load_inventory(args.inventory)
    nodes = select_nodes(nodes, args.node)
    missing = [n.name for n in nodes if not n.device]
    if missing:
        raise DeployError(f"Inventory 'device' is required for firmware: {', '.join(missing)}")
    images: dict[str, Path] = {}
    versions: dict[str, str] = {}
    for device_name in sorted({n.device for n in nodes}):
        if args.image:
            image = Path(args.image.get(device_name, "")).expanduser() if args.image.get(device_name) else None
            if not image:
                raise DeployError(f"No --image DEVICE=PATH for {device_name}")
            images[device_name] = image
            continue
        build_args = argparse.Namespace(
            device=device_name, target=None, version=args.version, feed_branch=args.feed_branch,
            dry_run=args.dry_run, build_timeout=args.build_timeout, image_type="sysupgrade",
            output_dir=args.output_dir, extra_packages=None, no_default_extra_packages=False,
            imagebuilder_dir=args.imagebuilder_dir, fixes_dir=getattr(args, "fixes_dir", None),
            pass_file=None, inject_pass=False,
            uci_defaults=None, list_devices=None, host=None, flash=False)
        path = dl.build_image(build_args)
        if path is not None:
            images[device_name] = Path(path)
    if args.dry_run:
        print("Dry run: no devices contacted.")
        return 0

    for device_name, image in images.items():
        meta = image.with_name(image.name + ".build.json")
        if meta.is_file():
            versions[device_name] = json.loads(meta.read_text(encoding="utf-8")).get("openwrt_version", "")
        if not image.is_file():
            raise DeployError(f"Image not found: {image}")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", image.name):
            raise DeployError(f"Unsupported image filename: {image.name}")

    groups = rollout_order(nodes)
    print("Upgrade plan (stop at the first failure):")
    for i, group in enumerate(groups, 1):
        print(f"  step {i}: {', '.join(f'{n.name} [{n.device}]' for n in group)}")
    print("Config is kept (sysupgrade -c). Aruba/Redmi have no hardware firmware rollback.")
    if not args.yes:
        if input(f"Type 'UPGRADE {len(nodes)} NODES' to proceed: ").strip() != f"UPGRADE {len(nodes)} NODES":
            print("Cancelled.")
            return 1

    for node in nodes:
        install_helpers(node)
    for group in groups:
        for node in group:
            if not upgrade_node(node, nodes, images[node.device], versions.get(node.device), args):
                print(f"STOPPED at {node}. Remaining nodes were not touched.")
                return 1
        if group is not groups[-1] and not args.yes_continue and group[0].canary:
            if input("Canary succeeded. Continue with the rest? [y/N] ").strip().lower() != "y":
                print("Stopped after canary.")
                return 0
    print("Firmware upgrade completed on all nodes.")
    return 0


def upgrade_node(node: Node, nodes: list[Node], image: Path, version: str | None, args) -> bool:
    print(f"== {node} ({node.device})")
    device = dl.resolve_device(node.device, None)
    try:
        dl.check_remote_board(node.host, device)
    except DeployError as error:
        print(f"  {error}")
        return False
    if not healthy(node_health(node, nodes), len(nodes)):
        print("  pre-upgrade health check failed")
        return False
    remote_image = f"/tmp/{image.name}"
    if not push(node, image, remote_image):
        print("  upload failed")
        return False
    import hashlib
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    check = remote(node, f"set -e; [ \"$(sha256sum {remote_image} | cut -d' ' -f1)\" = {digest} ]; "
                         f"sysupgrade -T {remote_image}", timeout=120)
    if check.returncode:
        print(f"  image rejected by the node: {check.stderr.strip() or 'checksum mismatch'}")
        remote(node, f"rm -f {remote_image}")
        return False
    remote(node, f"nohup sh -c 'sleep 2; exec sysupgrade -c -v {remote_image}' "
                 ">/tmp/libremesh-sysupgrade.log 2>&1 </dev/null &", timeout=20)
    print("  flashing, waiting for the node to reboot...")
    if not wait_for_reboot(node, args.reboot_timeout):
        print("  node did not come back in time")
        return False
    try:
        dl.check_remote_board(node.host, device)
    except DeployError as error:
        print(f"  {error}")
        return False
    release = remote(node, "cat /etc/openwrt_release").stdout
    if version and version not in release:
        print(f"  unexpected version after upgrade (wanted {version})")
        return False
    deadline = time.time() + args.settle
    while time.time() < deadline:
        if healthy(node_health(node, nodes), len(nodes)):
            print("  upgraded and healthy")
            return True
        time.sleep(10)
    print("  node is up but not healthy")
    return False


# ---------------------------------------------------------------- other

def cmd_status(args: argparse.Namespace) -> int:
    nodes, _ = load_inventory(args.inventory)
    nodes = select_nodes(nodes, args.node)

    def probe(node: Node) -> str:
        result = remote(node, f"{HELPER} status; {HELPER} health {peers_for(node, nodes)}", timeout=60)
        if result.returncode:
            return "UNREACHABLE (or helper not installed: run a config command first)"
        info = kv(result.stdout)
        return (f"{info.get('release', '?')} | health={info.get('health', '?')} "
                f"peers={info.get('peers_ok', '?')}/{info.get('peers_total', '?')} "
                f"pending={info.get('pending') or '-'}")
    for name, line in parallel(nodes, probe).items():
        print(f"{name:20} {line}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    nodes, _ = load_inventory(args.inventory)
    nodes = select_nodes(nodes, args.node)
    if not ID_RE.fullmatch(args.id):
        raise DeployError("Invalid change ID")
    results = parallel(nodes, lambda n: remote(n, f"{HELPER} rollback {args.id}", timeout=90))
    for name, res in results.items():
        print(f"{name:20} {res.stdout.strip() or res.stderr.strip()}")
    return 0 if all(r.returncode == 0 for r in results.values()) else 1


def cmd_backup(args: argparse.Namespace) -> int:
    nodes, _ = load_inventory(args.inventory)
    nodes = select_nodes(nodes, args.node)
    args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    def backup(node: Node) -> str:
        out = args.output_dir / f"{node.name}-{stamp}.tar.gz"
        argv, env = transport(node, dl.SSH_CMD)
        try:
            with open(out, "wb") as handle:
                result = subprocess.run(
                    [*argv, node.host, "tar -czf - -C / etc/config"],
                    stdout=handle, stderr=subprocess.PIPE, timeout=120, check=False, env=env)
        except subprocess.TimeoutExpired:
            out.unlink(missing_ok=True)
            return "timeout"
        out.chmod(0o600)
        if result.returncode:
            out.unlink(missing_ok=True)
            return "FAILED"
        return str(out)
    for name, line in parallel(nodes, backup).items():
        print(f"{name:20} {line}")
    return 0


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--inventory", type=Path, default=Path("fleet-inventory.json"))
        p.add_argument("--node", action="append", help="limit to this node (repeatable)")
        p.add_argument("--pass-file", type=Path, default=dl.DEFAULT_PASS_FILE,
                       help="private file with optional FLEET_SSH_PASSWORD (default: .pass)")

    p = sub.add_parser("status", help="show release, health and pending changes")
    common(p)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("backup", help="save /etc/config of every node locally (mode 600)")
    common(p)
    p.add_argument("--output-dir", type=Path, default=Path("fleet-backups"))
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("rollback", help="roll back a pending change on all nodes")
    common(p)
    p.add_argument("id")
    p.set_defaults(fn=cmd_rollback)

    p = sub.add_parser("config", help="apply a config-safe or config-breaking change")
    common(p)
    p.add_argument("change", type=Path, help="change JSON file")
    p.add_argument("--id")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="start even if the mesh is unhealthy")
    p.add_argument("--no-safety-guard", action="store_true")
    p.add_argument("--window", type=int, default=600, help="seconds before automatic rollback")
    p.add_argument("--lead", type=int, default=45, help="seconds until the simultaneous apply")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("firmware", help="upgrade firmware, keeping configuration")
    common(p)
    p.add_argument("--version", default="latest")
    p.add_argument("--feed-branch", default="master")
    p.add_argument("--image", type=lambda s: dict([s.split("=", 1)]), action="append",
                   help="DEVICE=PATH prebuilt image instead of building (repeatable)")
    p.add_argument("--output-dir", type=Path, default=ROOT / "firmware-output")
    p.add_argument("--imagebuilder-dir", type=Path, default=None)
    p.add_argument("--fixes-dir", type=Path, default=None,
                   help="alternate model fixes directory for images built locally")
    p.add_argument("--build-timeout", type=int, default=3600)
    p.add_argument("--reboot-timeout", type=int, default=600)
    p.add_argument("--settle", type=int, default=300)
    p.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    p.add_argument("--yes-continue", action="store_true", help="do not pause after the canary")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_firmware)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if getattr(args, "image", None) is not None and isinstance(args.image, list):
        merged = {}
        for item in args.image:
            merged.update(item)
        args.image = merged
    try:
        load_passwords(args.pass_file)
        return args.fn(args)
    except DeployError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Pending changes roll back by themselves unless confirmed.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
