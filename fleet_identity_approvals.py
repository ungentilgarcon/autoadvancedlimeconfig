"""Explicit local approvals for authenticated discovery hostname mismatches."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from fleet_archive import ArchiveError

APPROVALS = Path("fleet-discovery-approvals.json")
MAX_BYTES = 1024 * 1024
KEY_LINE = re.compile(r"^debug1: Server host key: ([^\s]+) (SHA256:[A-Za-z0-9+/]+={0,2})$", re.M)


@dataclass(frozen=True)
class Approval:
    advertised: str
    address: str
    hostname: str
    board: str
    key_type: str
    fingerprint: str


def load() -> list[Approval]:
    try:
        fd = os.open(APPROVALS, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise ArchiveError(f"Cannot open identity approvals: {error}") from error
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ArchiveError("Identity approvals must be an owner-only regular file")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ArchiveError("Identity approvals file exceeds size limit")
    try:
        records = json.loads(data)
        if not isinstance(records, list) or len(records) > 512:
            raise ValueError("invalid approval list")
        result = []
        for item in records:
            if not isinstance(item, dict) or set(item) != set(Approval.__dataclass_fields__):
                raise ValueError("invalid approval")
            if any(not isinstance(value, str) or not value or len(value) > 253 for value in item.values()):
                raise ValueError("invalid approval field")
            result.append(Approval(**item))
        return result
    except (ValueError, TypeError) as error:
        raise ArchiveError(f"Invalid identity approvals file: {error}") from error


def save(approval: Approval) -> None:
    records = [item for item in load()
               if item.advertised.lower() != approval.advertised.lower()
               and item.address != approval.address]
    records.append(approval)
    if len(records) > 512:
        raise ArchiveError("Too many identity approvals")
    data = json.dumps([asdict(item) for item in records], indent=2).encode() + b"\n"
    if len(data) > MAX_BYTES:
        raise ArchiveError("Identity approvals file exceeds size limit")
    path = APPROVALS.absolute()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".fleet-identity-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, path)
    except OSError as error:
        raise ArchiveError(f"Cannot save identity approval: {error}") from error
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def identify(fleet, node, advertised: str) -> Approval:
    argv, env = fleet.transport(node, [*fleet.dl.SSH_CMD, "-v"])
    env = {**(env if env is not None else os.environ), "LC_ALL": "C"}
    try:
        result = subprocess.run(
            [*argv, node.host, "command -v lime-config >/dev/null && ubus call system board"],
            capture_output=True, text=True, timeout=30, check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ArchiveError(f"{node}: SSH identity verification failed: {error}") from error
    if result.returncode:
        raise ArchiveError(f"{node}: trusted SSH or LibreMesh identity verification failed")
    matches = KEY_LINE.findall(result.stderr)
    if len(matches) != 1:
        raise ArchiveError(f"{node}: could not establish the authenticated SSH host key")
    try:
        board = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise ArchiveError(f"{node}: invalid board response") from error
    if not isinstance(board, dict) or any(
        not isinstance(board.get(field), str) or not board[field].strip()
        for field in ("hostname", "board_name")
    ):
        raise ArchiveError(f"{node}: hostname or board is missing")
    return Approval(advertised, str(node.mesh_ip), board["hostname"].strip(),
                    board["board_name"].strip(), *matches[0])


def verify_nodes(fleet, nodes: list) -> None:
    approved = [node for node in nodes if getattr(node, "identity_approval", None) is not None]
    if not approved:
        return
    records = load()
    for node in approved:
        approval = node.identity_approval
        if approval not in records or identify(fleet, node, approval.advertised) != approval:
            raise ArchiveError(f"{node}: locally approved router identity changed")
