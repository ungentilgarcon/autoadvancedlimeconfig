"""Discover LibreMesh routers from a trusted seed's shared DNS records."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

from fleet_archive import ArchiveError

HOSTNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
MAX_CANDIDATES = 512
DNS_COMMAND = (
    "f=/tmp/hosts/shared-state-dnsmasq_hosts; "
    "test -r \"$f\" || { echo 'LibreMesh shared DNS records unavailable' >&2; exit 1; }; "
    "[ \"$(wc -c < \"$f\")\" -le 1048576 ] || "
    "{ echo 'LibreMesh shared DNS records too large' >&2; exit 1; }; cat \"$f\""
)
MESH_COMMAND = "command -v lime-config >/dev/null"


def parse_shared_dns(text: str) -> tuple[dict[str, str], dict[str, str]]:
    records: dict[str, set[str]] = {}
    canonical: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.partition("#")[0].split()
        if not fields:
            continue
        try:
            address = ipaddress.ip_address(fields[0])
        except ValueError as error:
            raise ArchiveError("Invalid address in LibreMesh shared DNS records") from error
        if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
            continue
        if len(fields) < 2:
            raise ArchiveError("Invalid LibreMesh shared DNS record")
        for hostname in fields[1:]:
            if not HOSTNAME.fullmatch(hostname) or len(hostname) > 253:
                raise ArchiveError("Invalid hostname in LibreMesh shared DNS records")
            hostname = canonical.setdefault(hostname.lower(), hostname)
            records.setdefault(hostname, set()).add(str(address))
            if len(records) > MAX_CANDIDATES:
                raise ArchiveError("Too many LibreMesh shared DNS candidates")
    result = {}
    ambiguous = {}
    for hostname, addresses in records.items():
        ipv4 = [address for address in addresses if ipaddress.ip_address(address).version == 4]
        preferred = ipv4 or sorted(addresses)
        if len(preferred) != 1:
            ambiguous[hostname] = ", ".join(sorted(preferred))
            continue
        result[hostname] = preferred[0]
    if not result and not ambiguous:
        raise ArchiveError("LibreMesh shared DNS records are empty")
    return result, ambiguous


def discover(fleet, inventory: list, seed_host: str, inspect_inventory):
    if seed_host and not fleet.HOST_RE.fullmatch(seed_host):
        raise ArchiveError("Invalid mesh seed SSH host")
    seeds = [fleet.Node({"name": "mesh-seed", "host": seed_host})] if seed_host else inventory
    if not seeds:
        raise ArchiveError("Enter a mesh seed SSH host or provide an inventory")
    errors = []
    records = None
    ambiguous = {}
    seed = None
    for candidate in seeds:
        try:
            response = fleet.remote(candidate, DNS_COMMAND, timeout=20)
        except (fleet.DeployError, OSError) as error:
            errors.append(f"{candidate.host}: {error}")
            continue
        if response.returncode:
            errors.append(f"{candidate.host}: {(response.stderr.strip() or 'SSH query failed')[:300]}")
            continue
        records, ambiguous = parse_shared_dns(response.stdout)
        seed = candidate
        break
    if records is None or seed is None:
        raise ArchiveError("Mesh discovery failed: " + "; ".join(errors))

    details = inspect_inventory(inventory)
    seed_name = None
    if seed_host:
        seed_info = inspect_inventory([seed])[0]
        seed_name = seed_info.get("hostname")
        if seed_info["status"] != "available" or not isinstance(seed_name, str):
            raise ArchiveError(f"Cannot identify mesh seed: {seed_info.get('error') or 'hostname missing'}")
        if any(name.lower() == seed_name.lower() for name in ambiguous):
            raise ArchiveError("Mesh seed has ambiguous shared DNS addresses")
        if not any(name.lower() == seed_name.lower() for name in records):
            raise ArchiveError("Mesh seed hostname is missing from shared DNS records")
    names = {node.name.lower() for node in inventory}
    names.update(item["hostname"].lower() for item in details if item.get("hostname"))
    addresses = {str(node.mesh_ip) for node in inventory if node.mesh_ip}
    addresses.update(node.host.rsplit("@", 1)[-1] for node in inventory)
    candidates = []
    for name, address in sorted(records.items(), key=lambda item: item[0].lower()):
        if name.lower() in names or address in addresses:
            continue
        host = seed_host if seed_name and name.lower() == seed_name.lower() else f"root@{address}"
        candidates.append(fleet.Node({"name": name, "host": host, "mesh_ip": address}))

    observed = inspect_inventory(candidates)
    ambiguous_details = [
        {"name": name, "host": addresses, "status": "unavailable", "model": None,
         "model_id": None, "board": None, "hostname": None, "source": "discovered",
         "error": "Multiple shared DNS addresses; verify the node manually"}
        for name, addresses in sorted(ambiguous.items(), key=lambda item: item[0].lower())
        if name.lower() not in names
    ]
    eligible = []
    for node, item in zip(candidates, observed):
        if item["status"] == "available":
            hostname = item.get("hostname")
            if not isinstance(hostname, str) or hostname.lower() != node.name.lower():
                item["status"] = "unavailable"
                item["error"] = "SSH router hostname does not match the shared DNS record"
            else:
                eligible.append(node)
        item["source"] = "discovered"

    def check_mesh(node):
        try:
            check = fleet.remote(node, MESH_COMMAND, timeout=20)
        except (fleet.DeployError, OSError) as error:
            return str(error)
        if check.returncode:
            return check.stderr.strip()[:300] or "LibreMesh not detected or SSH failed"
        return None

    checks = fleet.parallel(eligible, check_mesh)
    verified = []
    for node, item in zip(candidates, observed):
        if node.name in checks:
            if checks[node.name] is not None:
                item["status"] = "unavailable"
                item["error"] = checks[node.name]
            else:
                verified.append(node)
    for item in details:
        item["source"] = "inventory"
    return inventory + verified, details + observed + ambiguous_details


def load_inventory(fleet, path: str) -> list:
    if not path:
        return []
    nodes, _ = fleet.load_inventory(Path(path))
    return nodes
