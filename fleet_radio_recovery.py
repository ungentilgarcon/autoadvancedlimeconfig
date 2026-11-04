"""Prepare and activate the AP-303 radio recovery workaround."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import fleet_file_deploy as deployment
import fleet_identity_approvals as identity
from fleet_archive import ArchiveError, Change, Entry, MAX_FILE_BYTES

BOARD = "aruba,ap-303"
ROOT = Path(__file__).resolve().parent / "fixes/ipq40xx/generic/aruba_ap-303/files"
CONFIG = "etc/config/picot-radio-recovery"
FILES = (
    (CONFIG, 0o644),
    ("etc/init.d/picot-radio-recovery", 0o755),
    ("usr/sbin/picot-radio-recovery", 0o755),
)


def prepare(nodes: list, inspect_inventory) -> tuple[list[deployment.NodePlan], dict[str, str]]:
    if not nodes:
        raise ArchiveError("No AP-303 targets selected")
    try:
        addresses = [ipaddress.ip_address(node.mesh_ip) for node in nodes]
    except (ValueError, TypeError) as error:
        raise ArchiveError("Every target needs a valid mesh IP") from error
    if len(set(addresses)) != len(addresses):
        raise ArchiveError("Every target needs a distinct mesh IP")
    details = inspect_inventory(nodes)
    if len(details) != len(nodes) or any(
        item.get("status") != "available" or item.get("board") != BOARD
        or not isinstance(item.get("hostname"), str) or not item["hostname"].strip()
        for item in details
    ):
        raise ArchiveError("Every selected target must be an available AP-303 with a hostname")
    hostnames = {node.name: item["hostname"] for node, item in zip(nodes, details)}
    desired = {}
    for path, mode in FILES:
        source = ROOT / path
        if source.is_symlink() or not source.is_file():
            raise ArchiveError(f"Missing or invalid workaround file: {source}")
        data = source.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise ArchiveError(f"Workaround file exceeds size limit: {source}")
        desired[path] = Entry("file", data, mode)
    plans = []
    for node in nodes:
        changes = []
        for path, _ in FILES:
            before = deployment.read_node_file(node, path)
            after = before if path == CONFIG and before is not None else desired[path]
            changes.append(Change(path, before, after))
        plan = deployment.preflight([node], changes)[0]
        if any(item.before != change.original or item.after != change.edited
               for item, change in zip(plan.files, changes)):
            raise ArchiveError(f"{node}: workaround files changed during the dry run; retry")
        plans.append(plan)
    return plans, hostnames


def activate(nodes: list) -> tuple[list[str], list[str]]:
    started, disabled = [], []
    for index, node in enumerate(nodes):
        command = (
            "set -eu; command -v flock >/dev/null; "
            "exec 9>/root/libremesh-failsafe/operation.lock; flock -n 9; "
            "test ! -s /root/libremesh-failsafe/cfg-pending; "
            "enabled=$(uci -q get picot-radio-recovery.settings.enabled) || "
            "{ echo 'missing radio recovery enabled setting' >&2; exit 1; }; "
            'case "$enabled" in '
            "0) echo disabled ;; "
            "1) /etc/init.d/picot-radio-recovery enable; "
            "/etc/init.d/picot-radio-recovery restart; echo started ;; "
            "*) echo 'invalid radio recovery enabled setting' >&2; exit 1 ;; esac"
        )
        try:
            identity.verify_nodes(deployment.fleet, [node])
            output = deployment._call(node, command, timeout=60).splitlines()
            result = output[-1].strip() if output else ""
            if result not in ("started", "disabled"):
                raise ArchiveError(f"{node}: unexpected service activation response")
        except (ArchiveError, deployment.fleet.DeployError, OSError) as error:
            remaining = ", ".join(item.name for item in nodes[index + 1:]) or "none"
            raise ArchiveError(
                f"Workaround files were deployed, but service activation failed on {node}: {error}. "
                f"Activation completed on: {', '.join(started + disabled) or 'none'}; "
                f"not attempted on: {remaining}. Inspect services manually."
            ) from error
        (started if result == "started" else disabled).append(node.name)
    return started, disabled
