"""Read-only preflight for applying reviewed archive changes to fleet nodes."""

from __future__ import annotations

import hashlib
import importlib.util
import ipaddress
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import fleet_identity_approvals as identity
from fleet_archive import (
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    ArchiveError,
    Change,
    Entry,
    merge_change,
    packages_from_text,
    safe_path,
)

ROOT = Path(__file__).resolve().parent
HELPER = "/usr/sbin/picot-files-sync"
PROTECTED = {
    "usr/sbin/picot-files-sync",
    "usr/sbin/picot-config-sync",
    "etc/init.d/picot-config-recovery",
}
spec = importlib.util.spec_from_file_location("fleet_libremesh", ROOT / "fleet-libremesh.py")
if spec is None or spec.loader is None:
    raise ImportError("Cannot load fleet controller")
fleet = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fleet
spec.loader.exec_module(fleet)


@dataclass(frozen=True)
class PlannedFile:
    change: Change
    before: Entry | None
    after: Entry | None


@dataclass(frozen=True)
class NodePlan:
    node: object
    files: tuple[PlannedFile, ...]
    packages: tuple[str, ...] = ()
    package_manager: str | None = None


def read_node_file(node, path: str) -> Entry | None:
    """Fetch one regular file without following symlinks or writing to the node."""
    relative = safe_path(path)
    absolute = PurePosixPath("/") / relative
    components = list(reversed(absolute.parents))[1:] + [absolute]
    checks = " ".join(
        f"if [ -L {shlex.quote(str(component))} ]; then "
        "echo 'symlink in path' >&2; exit 1; fi;"
        for component in components
    )
    quoted = shlex.quote(str(absolute))
    command = (
        f"set -eu; {checks} "
        f"if [ ! -e {quoted} ]; then echo MISSING; exit 0; fi; "
        f"if [ ! -f {quoted} ]; then echo 'not a regular file' >&2; exit 1; fi; "
        f"size=$(wc -c < {quoted}); "
        f"[ \"$size\" -le {MAX_FILE_BYTES} ] || "
        "{ echo 'file exceeds size limit' >&2; exit 1; }; "
        f"permissions=$(ls -ldn {quoted}); "
        "permissions=${permissions%% *}; "
        "printf 'FILE %s %s\\n' \"$permissions\" \"$size\"; "
        f"hexdump -v -e '1/1 \"%02x\"' {quoted}; printf '\\n'"
    )
    result = fleet.remote(node, command)
    if result.returncode:
        raise ArchiveError(f"{node}: cannot read /{relative}: {result.stderr.strip()}")
    lines = result.stdout.splitlines()
    if lines == ["MISSING"]:
        return None
    if not lines or not lines[0].startswith("FILE "):
        raise ArchiveError(f"{node}: invalid response reading /{relative}")
    try:
        _, mode, size = lines[0].split()
        size_int = int(size)
        if len(mode) != 10 or mode[0] != "-":
            raise ValueError("invalid mode")
        mode_int = 0
        for offset, (read, write, execute) in enumerate(((mode[1:4]), (mode[4:7]), (mode[7:10]))):
            if read not in "r-" or write not in "w-":
                raise ValueError("invalid mode")
            special = "sS" if offset < 2 else "tT"
            if execute not in "x-" + special:
                raise ValueError("invalid mode")
            bits = (4 if read == "r" else 0) | (2 if write == "w" else 0)
            bits |= 1 if execute in ("x", special[0]) else 0
            mode_int |= bits << (6 - 3 * offset)
            if execute in special:
                mode_int |= (0o4000, 0o2000, 0o1000)[offset]
        contents = bytes.fromhex("".join(lines[1:]))
    except ValueError as error:
        raise ArchiveError(f"{node}: invalid response reading /{relative}") from error
    if size_int < 0 or size_int > MAX_FILE_BYTES or len(contents) != size_int:
        raise ArchiveError(f"{node}: invalid file size reading /{relative}")
    if not 0 <= mode_int <= 0o7777:
        raise ArchiveError(f"{node}: invalid file mode reading /{relative}")
    return Entry("file", contents, mode_int)


def check_packages(node, packages: tuple[str, ...], expected: str | None = None) -> str:
    probe = fleet.remote(node, "if command -v apk >/dev/null; then echo apk; "
                              "elif command -v opkg >/dev/null; then echo opkg; "
                              "else echo 'neither apk nor opkg is available' >&2; exit 1; fi")
    manager = probe.stdout.strip()
    if probe.returncode:
        raise ArchiveError(f"{node}: package manager detection failed: "
                           f"{probe.stderr.strip() or 'remote command failed'}")
    if manager not in ("apk", "opkg"):
        raise ArchiveError(f"{node}: invalid package manager response")
    if expected is not None and manager != expected:
        raise ArchiveError(f"{node}: package manager changed since dry run")
    names = " ".join(shlex.quote(name) for name in packages)
    if manager == "apk":
        command = f"apk --simulate add -- {names}"
    else:
        command = (f"for pkg in {names}; do "
                   'info=$(opkg info "$pkg") || exit 1; '
                   'printf "%s\\n" "$info" | grep -Fx "Package: $pkg" >/dev/null || '
                   '{ echo "Package not found: $pkg" >&2; exit 1; }; done')
    result = fleet.remote(node, command, timeout=120)
    if result.returncode:
        detail = (result.stderr.strip() or result.stdout.strip() or "package check failed")[-2000:]
        raise ArchiveError(f"{node}: {manager} package preflight failed: {detail}")
    return manager


def preflight(nodes: list, changes: list[Change], packages: tuple[str, ...] = ()) -> list[NodePlan]:
    if not nodes:
        raise ArchiveError("No nodes selected")
    if not changes and not packages:
        raise ArchiveError("No changed files or packages to deploy")
    if tuple(packages_from_text(",".join(packages))) != packages:
        raise ArchiveError("Invalid package list")
    for change in changes:
        if safe_path(change.path) in PROTECTED:
            raise ArchiveError(f"Cannot replace the active recovery helper: /{change.path}")
    addresses = []
    for node in nodes:
        try:
            addresses.append(ipaddress.ip_address(node.mesh_ip))
        except ValueError as error:
            raise ArchiveError(f"{node}: a valid mesh_ip is required") from error
    if len(set(addresses)) != len(addresses):
        raise ArchiveError("Every node must have a distinct mesh_ip")
    plans = []
    for node in nodes:
        pending = fleet.remote(node, "command -v flock >/dev/null && "
                                     "test ! -s /root/libremesh-failsafe/cfg-pending")
        if pending.returncode:
            raise ArchiveError(f"{node}: flock unavailable, pending change, or SSH unavailable")
        files = []
        total = 0
        for change in changes:
            before = read_node_file(node, change.path)
            after = merge_change(change, before)
            total += (len(before.data) if before else 0) + (len(after.data) if after else 0)
            if total > MAX_TOTAL_BYTES:
                raise ArchiveError(f"{node}: merged files exceed the size limit")
            files.append(PlannedFile(change, before, after))
        manager = check_packages(node, packages) if packages else None
        plans.append(NodePlan(node, tuple(files), packages, manager))
    return plans


def _call(node, command: str, *, timeout: int = 60) -> str:
    result = fleet.remote(node, command, timeout=timeout)
    if result.returncode:
        raise ArchiveError(f"{node}: {result.stderr.strip() or 'remote command failed'}")
    return result.stdout


def _digest(entry: Entry | None) -> str:
    return hashlib.sha256(entry.data).hexdigest() if entry else "-"


def _mode(entry: Entry | None) -> str:
    return format(entry.mode, "o") if entry else "-"


def _manifest(plan: NodePlan) -> bytes:
    return "".join(
        f"{item.change.operation}|{item.change.path}|{_digest(item.before)}|"
        f"{_mode(item.before)}|{_digest(item.after)}|{_mode(item.after)}\n"
        for item in plan.files
    ).encode()


def _install(node, change_id: str) -> None:
    helpers = (
        (ROOT / "picot-config-sync", "/usr/sbin/picot-config-sync"),
        (ROOT / "picot-files-sync", HELPER),
        (ROOT / "picot-config-recovery", "/etc/init.d/picot-config-recovery"),
    )
    commands = []
    for local, destination in helpers:
        temporary = f"/tmp/picot-helper-{change_id}-{local.name}"
        if not fleet.push(node, local, temporary):
            raise ArchiveError(f"{node}: helper upload failed")
        commands.append(f"chmod 755 {temporary}; mv {temporary} {destination}")
    _call(node, "set -e; mkdir -p /root/libremesh-failsafe; "
          "command -v flock >/dev/null; exec 9>/root/libremesh-failsafe/operation.lock; "
          "flock -x 9; test ! -s /root/libremesh-failsafe/cfg-pending; "
          + "; ".join(commands) + "; /etc/init.d/picot-config-recovery enable")


def _stage(plan: NodePlan, change_id: str, mode: str, work: Path) -> None:
    node = plan.node
    remote_dir = f"/tmp/picot-files-{change_id}"
    _call(node, f"umask 077; mkdir -m 700 {remote_dir}")
    local_dir = work / node.name
    local_dir.mkdir(mode=0o700)
    manifest = local_dir / "manifest"
    manifest.write_bytes(_manifest(plan))
    for item in plan.files:
        if item.after:
            path = local_dir / f"data-{_digest(item.after)}"
            path.write_bytes(item.after.data)
    for path in local_dir.iterdir():
        if not fleet.push(node, path, f"{remote_dir}/{path.name}"):
            raise ArchiveError(f"{node}: staged file upload failed")
    _call(node, f"{HELPER} stage {change_id} {remote_dir} {mode}")


def _health(node, nodes: list) -> bool:
    info = fleet.node_health(node, nodes)
    return bool(info and info.get("health") == "ok"
                and info.get("peers_total") == str(len(nodes) - 1)
                and info.get("peers_ok") == info.get("peers_total"))


def install_packages(plan: NodePlan) -> None:
    manager = plan.package_manager
    if not plan.packages or manager not in ("apk", "opkg"):
        raise ArchiveError(f"{plan.node}: invalid package installation plan")
    names = " ".join(shlex.quote(name) for name in plan.packages)
    install = f"apk add -- {names}" if manager == "apk" else f"opkg install {names}"
    result = fleet.remote(plan.node, "set -e; mkdir -p /root/libremesh-failsafe; "
                          "exec 9>/root/libremesh-failsafe/operation.lock; flock -x 9; "
                          "test ! -s /root/libremesh-failsafe/cfg-pending; "
                          f"{install} 9>&-", timeout=600)
    if result.returncode:
        detail = (result.stderr.strip() or result.stdout.strip() or "remote command failed")[-2000:]
        raise ArchiveError(f"{plan.node}: {manager} installation failed: {detail}")


def _status(node, change_id: str) -> dict[str, str]:
    result = fleet.remote(node, f"{HELPER} status {change_id}")
    return fleet.kv(result.stdout) if result.returncode == 0 else {}


def _recover(nodes: list, change_id: str) -> list[str]:
    errors = []
    for node in nodes:
        status = _status(node, change_id)
        if status.get("pending") == f"files:{change_id}":
            action = "rollback"
        elif status.get("state") in ("staged", "armed"):
            action = "cancel"
        elif status.get("state") in ("rolledback", "cancelled", "missing"):
            action = None
        else:
            errors.append(f"{node}: state unknown or already confirmed")
            action = None
        if action:
            result = fleet.remote(node, f"{HELPER} {action} {change_id}", timeout=90)
            if (result.returncode and action == "cancel"
                    and _status(node, change_id).get("pending") == f"files:{change_id}"):
                action = "rollback"
                result = fleet.remote(node, f"{HELPER} rollback {change_id}", timeout=90)
            if result.returncode:
                errors.append(f"{node}: {action} failed; inspect node and watchdog")
        result = fleet.remote(node, f"rm -rf /tmp/picot-files-{change_id}")
        if result.returncode:
            errors.append(f"{node}: failed to clean temporary upload")
    return errors


def deploy(plans: list[NodePlan], mode: str, *, lead: int = 60, window: int = 600) -> str:
    if mode not in ("none", "reload"):
        raise ArchiveError("Invalid activation mode")
    if not 30 <= lead <= 600 or not 240 <= window <= 3600:
        raise ArchiveError("Invalid synchronization or rollback window")
    nodes = [plan.node for plan in plans]
    change_id = "web-" + uuid.uuid4().hex[:24]
    staged = []
    package_attempted = []
    package_completed = []
    try:
        identity.verify_nodes(fleet, nodes)
        for plan in plans:
            if plan.packages:
                check_packages(plan.node, plan.packages, plan.package_manager)
        for node in nodes:
            _install(node, change_id)
        unhealthy = [str(node) for node in nodes if not _health(node, nodes)]
        if unhealthy:
            raise ArchiveError("Preflight health failed: " + ", ".join(unhealthy))
        for plan in plans:
            if plan.packages:
                package_attempted.append(str(plan.node))
                install_packages(plan)
                package_completed.append(str(plan.node))
                if not all(_health(node, nodes) for node in nodes):
                    raise ArchiveError(f"Fleet health failed after package installation on {plan.node}")
        if not any(plan.files for plan in plans):
            return change_id
        with tempfile.TemporaryDirectory(prefix="fleet-files-") as directory:
            for plan in plans:
                staged.append(plan.node)
                _stage(plan, change_id, mode, Path(directory))
        identity.verify_nodes(fleet, nodes)
        offsets = fleet.parallel(nodes, fleet.clock_offset)
        target = time.time() + lead
        for node in nodes:
            epoch = round(target + offsets[node.name])
            _call(node, f"{HELPER} apply-at {change_id} {epoch} {window}")
        if time.time() >= target - 15:
            raise ArchiveError("Not enough time remains to safely cancel before apply")
        time.sleep(max(0, target - time.time()) + 5)
        deadline = time.monotonic() + window - 120
        while time.monotonic() < deadline:
            checks = fleet.parallel(nodes, lambda n: (
                _status(n, change_id).get("state") == "applied" and _health(n, nodes)
            ))
            if all(checks.values()):
                break
            time.sleep(10)
        else:
            raise ArchiveError("Fleet did not become healthy before the rollback deadline")
        for node in nodes:
            _call(node, f"{HELPER} extend {change_id} 600")
        failures = []
        for node in nodes:
            result = fleet.remote(node, f"{HELPER} confirm {change_id}")
            if result.returncode and _status(node, change_id).get("state") != "confirmed":
                failures.append(str(node))
        if failures:
            raise ArchiveError(
                "Confirmation was partial; nodes may diverge. Inspect all nodes, especially: "
                + ", ".join(failures)
            )
        return change_id
    except (ArchiveError, fleet.DeployError, OSError, subprocess.SubprocessError) as error:
        errors = _recover(staged, change_id)
        detail = f" Change ID: {change_id}."
        if package_attempted:
            detail += (" Package installation attempted on: " + ", ".join(package_attempted)
                       + "; completed on: " + (", ".join(package_completed) or "none")
                       + ". Packages and dependencies are not rolled back; a failed or timed-out "
                         "installation may be partial or still running. Inspect those nodes.")
        if errors:
            detail += " Recovery needs attention: " + "; ".join(errors)
        raise ArchiveError(str(error) + detail) from error
