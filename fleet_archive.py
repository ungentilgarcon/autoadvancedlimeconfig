"""Read and compare OpenWrt backup archives without extracting them."""

from __future__ import annotations

import difflib
import io
import re
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 10000
PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.-]*\Z")


class ArchiveError(ValueError):
    """A backup or requested change cannot be safely processed."""


@dataclass(frozen=True)
class Entry:
    kind: str
    data: bytes = b""
    mode: int = 0
    link: str = ""


@dataclass(frozen=True)
class Change:
    path: str
    original: Entry | None
    edited: Entry | None

    @property
    def operation(self) -> str:
        if self.original is None:
            return "add"
        if self.edited is None:
            return "delete"
        return "modify"


def supported_path(parts: tuple[str, ...]) -> bool:
    return (len(parts) >= 2 and parts[0] in ("etc", "opt")) or (
        len(parts) >= 3 and parts[:2] == ("usr", "sbin")
    )


def safe_path(name: str) -> str:
    if name.startswith("/") or "\x00" in name:
        raise ArchiveError("Archive contains an absolute or invalid path")
    parts = name.split("/")
    if ".." in parts:
        raise ArchiveError("Archive contains a parent-directory path")
    path = PurePosixPath(name)
    if not supported_path(path.parts):
        raise ArchiveError("Only files under /etc, /usr/sbin or /opt may be selected")
    if any(not re.fullmatch(r"[A-Za-z0-9_.+@-]+", part) for part in path.parts):
        raise ArchiveError("Archive path contains unsupported characters")
    return str(path)


def read_backup(contents: bytes) -> dict[str, Entry]:
    if len(contents) > MAX_ARCHIVE_BYTES:
        raise ArchiveError("Compressed backup exceeds the size limit")
    entries: dict[str, Entry] = {}
    seen: set[str] = set()
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(contents), mode="r:gz") as archive:
            for index, member in enumerate(archive):
                if index >= MAX_MEMBERS:
                    raise ArchiveError("Backup contains too many entries")
                if member.name.startswith("/") or ".." in member.name.split("/"):
                    raise ArchiveError("Backup contains an unsafe path")
                parts = PurePosixPath(member.name).parts
                if not supported_path(parts):
                    continue
                path = safe_path(member.name)
                if path in seen:
                    raise ArchiveError(f"Duplicate backup entry: /{path}")
                seen.add(path)
                if member.isdir():
                    continue
                if member.isfile():
                    if member.size > MAX_FILE_BYTES:
                        raise ArchiveError(f"Backup file is too large: /{path}")
                    total += member.size
                    if total > MAX_TOTAL_BYTES:
                        raise ArchiveError("Backup contents exceed the size limit")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ArchiveError(f"Could not read /{path}")
                    data = stream.read(MAX_FILE_BYTES + 1)
                    if len(data) != member.size:
                        raise ArchiveError(f"Truncated or oversized file: /{path}")
                    entries[path] = Entry("file", data, member.mode & 0o7777)
                elif member.issym() or member.islnk():
                    entries[path] = Entry("link", link=member.linkname)
                else:
                    entries[path] = Entry("special")
    except (OSError, EOFError, tarfile.TarError) as error:
        raise ArchiveError(f"Cannot read gzip backup: {error}") from error
    return entries


def compare(original: dict[str, Entry], edited: dict[str, Entry]) -> list[Change]:
    changes = []
    for path in sorted(original.keys() | edited.keys()):
        before, after = original.get(path), edited.get(path)
        if before == after:
            continue
        if (before and before.kind != "file") or (after and after.kind != "file"):
            raise ArchiveError(f"Changed links or special files are not supported: /{path}")
        changes.append(Change(path, before, after))
    return changes


def packages_from_text(value: str) -> list[str]:
    if not value.strip():
        return []
    names = [name.strip() for name in value.split(",")]
    if any(not PACKAGE_NAME.fullmatch(name) for name in names):
        raise ArchiveError("Package names must be comma-separated valid package identifiers")
    return list(dict.fromkeys(names))


def merge_change(change: Change, target: Entry | None) -> Entry | None:
    """Three-way merge a change, refusing overlapping or ambiguous changes."""
    before, after = change.original, change.edited
    if target is not None and target.kind != "file":
        raise ArchiveError(f"Target is not a regular file: /{change.path}")
    if before is None:
        if target is not None:
            raise ArchiveError(f"Target file already exists: /{change.path}")
        return after
    if after is None:
        if target != before:
            raise ArchiveError(f"Target differs from the original: /{change.path}")
        return None
    if target is None:
        raise ArchiveError(f"Target file is missing: /{change.path}")
    if target.mode == before.mode or target.mode == after.mode:
        mode = after.mode
    elif after.mode == before.mode:
        mode = target.mode
    else:
        raise ArchiveError(f"Permission conflict in /{change.path}")
    if before.data == after.data:
        return Entry("file", target.data, mode)
    if target.data == before.data:
        return Entry("file", after.data, mode)
    if b"\x00" in before.data or b"\x00" in after.data or b"\x00" in target.data:
        raise ArchiveError(f"Cannot merge a changed binary file: /{change.path}")
    with tempfile.TemporaryDirectory(prefix="fleet-merge-") as directory:
        paths = [Path(directory) / name for name in ("target", "original", "edited")]
        for path, data in zip(paths, (target.data, before.data, after.data)):
            path.write_bytes(data)
            path.chmod(0o600)
        try:
            result = subprocess.run(
                ["git", "merge-file", "-p", *map(str, paths)],
                capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ArchiveError(f"Could not merge /{change.path}: {error}") from error
        if result.returncode:
            raise ArchiveError(f"Merge conflict in /{change.path}")
        return Entry("file", result.stdout, mode)


def diff_summary(change: Change) -> tuple[int, int]:
    before = change.original.data.splitlines() if change.original else []
    after = change.edited.data.splitlines() if change.edited else []
    added = removed = 0
    for tag, a0, a1, b0, b1 in difflib.SequenceMatcher(None, before, after).get_opcodes():
        if tag != "equal":
            removed += a1 - a0
            added += b1 - b0
    return added, removed
