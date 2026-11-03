"""Staged, pilot-first firmware upgrades for the fleet UI."""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import fleet_file_deploy as deployment
import fleet_identity_approvals as identity
from fleet_archive import ArchiveError

fleet = deployment.fleet
MAX_IMAGE_BYTES = 128 * 1024 * 1024
REBOOT_TIMEOUT = 600
SETTLE_TIMEOUT = 300


@dataclass(frozen=True)
class FirmwarePlan:
    image: bytes
    filename: str
    digest: str
    board: str
    hostnames: dict[str, str]
    nodes: tuple


def decode_image(filename: object, encoded: object) -> tuple[str, bytes]:
    if (not isinstance(filename, str) or not filename or len(filename) > 255
            or "/" in filename or "\\" in filename or any(ord(char) < 32 for char in filename)
            or Path(filename).suffix.lower() not in (".bin", ".itb")):
        raise ArchiveError("Select a .bin or .itb firmware image with a valid filename")
    if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
        raise ArchiveError("Invalid or oversized firmware image")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ArchiveError("Firmware image is not valid base64") from error
    if not image or len(image) > MAX_IMAGE_BYTES:
        raise ArchiveError("Firmware image is empty or exceeds 128 MiB")
    return filename, image


def _call(node, command: str, *, timeout: int = 60) -> str:
    result = fleet.remote(node, command, timeout=timeout)
    if result.returncode:
        raise ArchiveError(f"{node}: {result.stderr.strip() or 'remote command failed'}")
    return result.stdout


def _remove(node, path: str) -> None:
    _call(node, f"rm -f -- {path}", timeout=20)


def _with_temporary_upload(node, local: Path, remote: str, action) -> None:
    try:
        if not fleet.push(node, local, remote):
            raise ArchiveError(f"{node}: upload failed")
        action()
    finally:
        original = sys.exc_info()[1]
        try:
            _remove(node, remote)
        except (ArchiveError, fleet.DeployError, OSError) as cleanup:
            raise ArchiveError(f"{node}: temporary file cleanup failed: {cleanup}"
                               + (f"; original error: {original}" if original else "")) from cleanup


def _health(node, nodes: tuple) -> bool:
    helper = Path(__file__).resolve().parent / "picot-config-sync"
    remote = f"/tmp/picot-firmware-health-{uuid.uuid4().hex}"
    peers = fleet.peers_for(node, list(nodes))
    result = {}

    def check():
        output = _call(node, f"sh {remote} health {peers}", timeout=max(40, len(nodes) * 5 + 15))
        result.update(fleet.kv(output))

    _with_temporary_upload(node, helper, remote, check)
    return (result.get("health") == "ok" and result.get("peers_total") == str(len(nodes) - 1)
            and result.get("peers_ok") == result.get("peers_total"))


def _check_identity(node, plan: FirmwarePlan, inspect_inventory) -> None:
    identity.verify_nodes(fleet, [node])
    item = inspect_inventory([node])[0]
    hostname = item.get("hostname")
    if (item["status"] != "available" or item["board"] != plan.board
            or not isinstance(hostname, str)
            or hostname.lower() != plan.hostnames[node.name].lower()):
        raise ArchiveError(f"{node}: board or hostname changed or is unavailable")


def _boot_id(node) -> str:
    value = _call(node, "cat /proc/sys/kernel/random/boot_id", timeout=15).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        raise ArchiveError(f"{node}: invalid boot ID")
    return value.lower()


def _image_test(node, image_path: Path, digest: str) -> None:
    remote = f"/tmp/picot-firmware-{uuid.uuid4().hex}{image_path.suffix}"

    def check():
        _call(node, f"set -e; test \"$(sha256sum {remote} | cut -d' ' -f1)\" = {digest}; "
                    f"sysupgrade -T {remote}", timeout=120)

    _with_temporary_upload(node, image_path, remote, check)


def prepare(nodes: list, filename: object, encoded: object, inspect_inventory) -> FirmwarePlan:
    filename, image = decode_image(filename, encoded)
    if not nodes:
        raise ArchiveError("No firmware targets selected")
    try:
        addresses = [ipaddress.ip_address(node.mesh_ip) for node in nodes]
    except (ValueError, TypeError) as error:
        raise ArchiveError("Every firmware target needs a valid mesh IP") from error
    if len(set(addresses)) != len(addresses):
        raise ArchiveError("Every firmware target needs a distinct mesh IP")
    details = inspect_inventory(nodes)
    if any(item["status"] != "available" or not isinstance(item.get("hostname"), str)
           or not item["hostname"] for item in details):
        raise ArchiveError("Every firmware target must report its board and hostname")
    boards = {item["board"] for item in details}
    if len(boards) != 1:
        raise ArchiveError("Firmware targets must all have the same live board model")
    ordered = tuple(node for group in fleet.rollout_order(nodes) for node in group)
    plan = FirmwarePlan(image, filename, hashlib.sha256(image).hexdigest(),
                        next(iter(boards)), {item["name"]: item["hostname"] for item in details}, ordered)
    with tempfile.TemporaryDirectory(prefix="fleet-firmware-") as directory:
        image_path = Path(directory) / ("image" + Path(filename).suffix.lower())
        image_path.write_bytes(image)
        for node in ordered:
            _call(node, "command -v sysupgrade >/dev/null || { echo 'sysupgrade is required' >&2; exit 1; }; "
                        "command -v flock >/dev/null || { echo 'flock is required' >&2; exit 1; }; "
                        "test ! -s /root/libremesh-failsafe/cfg-pending || "
                        "{ echo 'fleet configuration is pending' >&2; exit 1; }", timeout=20)
            if not _health(node, ordered):
                raise ArchiveError(f"{node}: pre-upgrade health check failed")
            _image_test(node, image_path, plan.digest)
    return plan


def flash_node(node, plan: FirmwarePlan, inspect_inventory) -> None:
    _check_identity(node, plan, inspect_inventory)
    if not _health(node, plan.nodes):
        raise ArchiveError(f"{node}: pre-upgrade health check failed")
    boot_id = _boot_id(node)
    remote = f"/tmp/picot-firmware-{uuid.uuid4().hex}{Path(plan.filename).suffix.lower()}"
    with tempfile.TemporaryDirectory(prefix="fleet-firmware-") as directory:
        image_path = Path(directory) / ("image" + Path(plan.filename).suffix.lower())
        image_path.write_bytes(plan.image)
        try:
            if not fleet.push(node, image_path, remote):
                raise ArchiveError(f"{node}: firmware upload failed")
            _call(node, f"set -e; test \"$(sha256sum {remote} | cut -d' ' -f1)\" = {plan.digest}; "
                        f"sysupgrade -T {remote}", timeout=120)
        except (ArchiveError, fleet.DeployError, OSError) as error:
            try:
                _remove(node, remote)
            except (ArchiveError, fleet.DeployError, OSError) as cleanup:
                raise ArchiveError(f"{node}: firmware cleanup failed: {cleanup}; original error: {error}") from cleanup
            raise
        try:
            _call(node, "set -eu; command -v flock >/dev/null; mkdir -p /root/libremesh-failsafe; "
                        "exec 9>/root/libremesh-failsafe/operation.lock; flock -n 9; "
                        "test ! -s /root/libremesh-failsafe/cfg-pending; "
                        f"test \"$(sha256sum {remote} | cut -d' ' -f1)\" = {plan.digest}; "
                        f"sysupgrade -T {remote}; "
                        + fleet.dl.sysupgrade_background_command(remote), timeout=120)
        except (ArchiveError, fleet.DeployError, OSError) as error:
            raise ArchiveError(f"{node}: firmware launch outcome is uncertain; do not retry or remove "
                               f"{remote} until the node is inspected: {error}") from error
    if not fleet.wait_for_reboot(node, REBOOT_TIMEOUT):
        raise ArchiveError(f"{node}: reboot was not observed or the node did not return; inspect "
                           "the node and /tmp/libremesh-sysupgrade.log before retrying")
    if _boot_id(node) == boot_id:
        raise ArchiveError(f"{node}: boot ID did not change; inspect manually")
    _check_identity(node, plan, inspect_inventory)
    deadline = time.monotonic() + SETTLE_TIMEOUT
    while time.monotonic() < deadline:
        if _health(node, plan.nodes):
            return
        time.sleep(10)
    raise ArchiveError(f"{node}: rebooted but did not become healthy; rollout stopped")
