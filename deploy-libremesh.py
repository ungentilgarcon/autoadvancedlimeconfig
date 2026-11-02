#!/usr/bin/env python3
"""Build current LibreMesh Selector images and stage them with a config rollback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SELECTOR = "https://firmware-selector.libremesh.org"
DEFAULT_EXTRAS_FILE = ROOT / "firmware-selector-packages.txt"
DEFAULT_PASS_FILE = ROOT / ".pass"
VLAN_SCRIPT = ROOT / "apply-libremesh-vlans.sh"
VLAN_RECOVERY_SCRIPT = ROOT / "picot-vlan-recovery"
AUTOWAN_SCRIPT = ROOT / "picot-autowan"
AUTOWAN_INIT = ROOT / "picot-autowan.init"
USER_AGENT = "libremesh-deploy/1.0"
DEVICES = {
    "redmi-ax6000-ubootmod": {
        "profile": "xiaomi_redmi-router-ax6000-ubootmod",
        "target": "mediatek/filogic",
        "board": "xiaomi,redmi-router-ax6000-ubootmod",
    },
    "aruba-ap-303": {
        "profile": "aruba_ap-303",
        "target": "ipq40xx/generic",
        "board": "aruba,ap-303",
    },
}


class DeployError(Exception):
    pass


def _ssh_command(program: str) -> list[str]:
    command = [program, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
    # Optional alternate ssh_config (used by the emulator testbed); the
    # security options above always stay in force.
    config = os.environ.get("LIBREMESH_SSH_CONFIG")
    if config:
        command[1:1] = ["-F", config]
    return command


SSH_CMD = _ssh_command("ssh")
SCP_CMD = [*_ssh_command("scp"), "-O"]


def normalize_board(name: str) -> str:
    return name.strip().replace(",", "_")


def resolve_device(name: str, target: str | None = None) -> dict:
    """Return a device description from a shortcut alias or any OpenWrt name.

    Any OpenWrt board name (aruba,ap-303) or Selector profile id (aruba_ap-303)
    is accepted; the build target is discovered from the Selector when omitted.
    """
    if name in DEVICES:
        return dict(DEVICES[name])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9,_.+-]*", name):
        raise DeployError(f"Invalid device name: {name!r}")
    if target is not None and not re.fullmatch(r"[a-z0-9_-]+/[a-z0-9_-]+", target):
        raise DeployError(f"Invalid --target: {target!r}")
    return {"profile": normalize_board(name), "target": target, "board": name}


def http_json(url: str, data: dict | None = None, timeout: int = 60) -> tuple[int, dict]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read())
        except (ValueError, OSError):
            payload = {"detail": error.reason}
        return error.code, payload
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise DeployError(f"Request failed for {url}: {error}") from error


def http_text(url: str, timeout: int = 60) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as error:
        raise DeployError(f"Request failed for {url}: {error}") from error


def js_fragment(source: str, pattern: str, opening: str) -> str:
    match = re.search(pattern, source)
    if not match:
        raise DeployError(f"Could not read Selector setting matching {pattern!r}")
    start = source.find(opening, match.start())
    if start < 0:
        raise DeployError(f"Malformed Selector setting matching {pattern!r}")
    closing = "]" if opening == "[" else "}"
    depth = 0
    in_string = False
    escaped = False
    for pos in range(start, len(source)):
        char = source[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return source[start : pos + 1]
    raise DeployError(f"Unterminated Selector setting matching {pattern!r}")


def js_literal(source: str, pattern: str, opening: str) -> object:
    fragment = js_fragment(source, pattern, opening)
    fragment = re.sub(r",(\s*[\]}])", r"\1", fragment)
    try:
        return json.loads(fragment)
    except json.JSONDecodeError as error:
        raise DeployError(
            f"Selector setting is not JSON-compatible: {error}"
        ) from error


def selector_config() -> dict:
    source = http_text(f"{SELECTOR}/config.js")
    asu_match = re.search(r'\basu_url\s*:\s*"([^"]+)"', source)
    image_match = re.search(r'\bimage_url\s*:\s*"([^"]+)"', source)
    if not asu_match or not image_match:
        raise DeployError("Selector config is missing its ASU or image URL")

    repo_fragment = js_fragment(source, r"\basu_repositories\s*:", "{")
    repos_obj = {
        entry.group(1): json.loads(entry.group(2))
        for entry in re.finditer(
            r'(?m)^\s*([A-Za-z0-9_]+)\s*:\s*("(?:\\.|[^"])*")\s*,?',
            repo_fragment[1:-1],
        )
    }
    if not repos_obj:
        raise DeployError("Selector ASU repositories are malformed")

    keys = js_literal(source, r"\basu_repository_keys\s*:", "[")
    lm_block = {
        "branches": js_literal(source, r"\bbranches\s*:", "["),
        "default_packages": js_literal(source, r"\bdefault_packages\s*:", "["),
        "generic_save_space": js_literal(source, r"\bgeneric_save_space\s*:", "["),
        "flavors": {
            "default": js_literal(source, r"\bdefault\s*:", "["),
        },
    }
    return {
        "asu_url": asu_match.group(1).rstrip("/"),
        "image_url": image_match.group(1).rstrip("/"),
        "repositories": repos_obj,
        "repository_keys": keys,
        "libremesh": lm_block,
    }


def unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def read_extra_packages(args: argparse.Namespace) -> list[str]:
    packages: list[str] = []
    if not args.no_default_extra_packages:
        if not DEFAULT_EXTRAS_FILE.is_file():
            raise DeployError(f"Extra-package list not found: {DEFAULT_EXTRAS_FILE}")
        for line in DEFAULT_EXTRAS_FILE.read_text(encoding="utf-8").splitlines():
            packages.extend(line.split("#", 1)[0].split())
    for group in args.extra_packages:
        packages.extend(shlex.split(group))
    for package in packages:
        if not re.fullmatch(r"-?[A-Za-z0-9][A-Za-z0-9+_.-]*", package):
            raise DeployError(f"Invalid package name: {package!r}")
    return unique(packages)


def selector_package_changes(version: str, packages: list[str]) -> list[str]:
    changes: list[str] = []
    rules = [
        ("kmod-ath10k-ct-smallbuffers", [
            "-kmod-ath10k", "-kmod-ath10k-ct",
            "-kmod-ath10k-ct-smallbuffers", "kmod-ath10k-smallbuffers",
        ]),
        ("kmod-ath10k-ct", [
            "-kmod-ath10k-ct", "-kmod-ath10k-ct-smallbuffers", "kmod-ath10k",
        ]),
        ("-kmod-ath10k-ct", ["-kmod-ath10k"]),
    ]
    for package in packages:
        for source, replacements in rules:
            if package == source:
                changes.extend(replacements)
        match = re.fullmatch(r"(-?)ath10k-firmware-qca(.*)-ct(.*)", package)
        if match:
            if match.group(1):
                changes.append(f"ath10k-firmware-qca{match.group(2)}")
            else:
                changes.extend(
                    [
                        f"-ath10k-firmware-qca{match.group(2)}-ct{match.group(3)}",
                        f"ath10k-firmware-qca{match.group(2)}",
                    ]
                )
    if re.fullmatch(r"(24\.10\.[0-5]|23\.05\.\d|)", version):
        if "kmod-ath9k" in packages:
            changes.append("wifi-unstuck-wa")
        if "-kmod-ath9k" in packages:
            changes.append("-wifi-unstuck-wa")
    return unique(changes)


def asu_repositories(
    templates: dict, feed_branch: str, version: str, target: str, arch: str
) -> dict[str, str]:
    openwrt_branch_match = re.match(r"^(\d+\.\d+)", version)
    openwrt_branch = openwrt_branch_match.group(1) if openwrt_branch_match else version
    openwrt_feeds_branch = "main" if version == "SNAPSHOT" else openwrt_branch
    target_name, _, subtarget = target.partition("/")
    values = {
        "feed_branch": feed_branch,
        "openwrt_branch": openwrt_branch,
        "openwrt_branch_lime_feeds": openwrt_feeds_branch,
        "openwrt_version": version,
        "openwrt_arch": arch,
        "target": target_name,
        "subtarget": subtarget,
    }
    return {
        name: re.sub(
            r"\{([a-z_]+)\}",
            lambda match: values.get(match.group(1), match.group(0)),
            url,
        )
        for name, url in templates.items()
    }


def fetch_profile(config: dict, version: str, device: dict) -> tuple[dict, dict, dict]:
    if version == "SNAPSHOT":
        base = f"{config['image_url']}/snapshots"
    else:
        base = f"{config['image_url']}/releases/{version}"
    overview = http_json(f"{base}/.overview.json")[1]
    matches = [
        profile
        for profile in overview.get("profiles", [])
        if profile.get("id") == device["profile"]
        and (device["target"] is None or profile.get("target") == device["target"])
    ]
    if len(matches) > 1:
        targets = ", ".join(sorted(str(match.get("target")) for match in matches))
        raise DeployError(
            f"{device['profile']} exists for several targets ({targets}); "
            "choose one with --target"
        )
    if len(matches) != 1:
        raise DeployError(
            f"{device['profile']} is not available in OpenWrt {version}"
            + (f" for {device['target']}" if device["target"] else "")
        )
    device["target"] = matches[0]["target"]
    profile_index = http_json(
        f"{base}/targets/{device['target']}/profiles.json"
    )[1]
    profile = profile_index.get("profiles", {}).get(device["profile"])
    if not profile:
        raise DeployError(f"Profile details missing for {device['profile']}")
    supported = profile.get("supported_devices") or []
    if supported and normalize_board(device["board"]) not in {
        normalize_board(item) for item in supported
    }:
        device["board"] = supported[0]
    return overview, profile_index, profile


def build_image(args: argparse.Namespace) -> Path | None:
    if args.inject_pass and args.uci_defaults:
        raise DeployError(
            "--inject-pass cannot be combined with --uci-defaults; "
            "credentials are only embedded by the local ImageBuilder path"
        )
    config = selector_config()
    status, versions = http_json(f"{config['image_url']}/.versions.json")
    if status != 200:
        raise DeployError(f"Could not retrieve OpenWrt release list (HTTP {status})")
    version = versions.get("stable_version") if args.version == "latest" else args.version
    if not version or not re.fullmatch(r"[A-Za-z0-9._-]+", version):
        raise DeployError(f"Invalid or unavailable release version: {version!r}")

    feed_branches = config["libremesh"].get("branches", [])
    if args.feed_branch not in feed_branches:
        raise DeployError(
            f"Unsupported LibreMesh feed branch {args.feed_branch!r}; "
            f"Selector supports: {', '.join(feed_branches)}"
        )
    device = resolve_device(args.device, getattr(args, "target", None))
    overview, profile_index, profile = fetch_profile(config, version, device)
    packages = unique(
        list(profile_index.get("default_packages", []))
        + list(profile.get("device_packages", []))
    )
    mapped_status, mapped = http_json(f"{SELECTOR}/device_packages.json")
    if mapped_status == 200 and isinstance(mapped, dict):
        mapped_packages = mapped.get(device["profile"], [])
        if not mapped_packages:
            mapped_packages = mapped.get(device["profile"].replace("_", ","), [])
        if isinstance(mapped_packages, list):
            packages.extend(str(item) for item in mapped_packages)
    elif mapped_status != 404:
        raise DeployError(
            f"Could not read Selector device package map (HTTP {mapped_status})"
        )
    packages.extend(config.get("asu_extra_packages", []))
    packages.extend(read_extra_packages(args))
    asu_packages = unique(packages)

    lime_config = config["libremesh"]
    package_sources = unique(
        list(profile_index.get("default_packages", []))
        + list(profile.get("device_packages", []))
    )
    lime_packages = unique(
        list(lime_config.get("default_packages", []))
        + list(lime_config.get("generic_save_space", []))
        + selector_package_changes(version, package_sources)
        + list(lime_config.get("flavors", {}).get("default", []))
    )

    target = device["target"]
    arch = profile_index.get("arch_packages")
    if not arch:
        raise DeployError(f"Missing package architecture for {target}")
    request = {
        "profile": device["profile"],
        "target": target,
        "packages": asu_packages + lime_packages,
        "defaults": "",
        "version_code": profile_index.get("version_code", ""),
        "version": version,
        "diff_packages": True,
        "client": "libremesh-deploy/1.0",
        "repositories": asu_repositories(
            config["repositories"], args.feed_branch, version, target, arch
        ),
        "repository_keys": config["repository_keys"],
        "repositories_mode": "append",
    }
    if args.uci_defaults:
        defaults_path = Path(args.uci_defaults).expanduser().resolve()
        if not defaults_path.is_file():
            raise DeployError(f"UCI-defaults file not found: {defaults_path}")
        defaults = defaults_path.read_text(encoding="utf-8")
        if len(defaults) > 256_000:
            raise DeployError("UCI-defaults file is larger than 256 KB")
        request["defaults"] = defaults

    if args.dry_run:
        print(f"Selector stable release: {version}")
        print(f"Device: {args.device} ({target}/{device['profile']})")
        print(f"LibreMesh feed: {args.feed_branch}")
        print(f"OpenWrt packages: {' '.join(asu_packages) or '(none)'}")
        print(f"LibreMesh packages: {' '.join(lime_packages) or '(none)'}")
        print(f"Resolved repositories: {json.dumps(request['repositories'], sort_keys=True)}")
        if args.inject_pass:
            values = read_equipment_pass(args.pass_file, require_vlan_keys=False)
            pass_fields_for_device(device, values)
            print("Password injection: local ImageBuilder; values are not sent to ASU.")
        print("Dry run: no image build was requested.")
        return None

    if args.inject_pass:
        values = read_equipment_pass(args.pass_file, require_vlan_keys=False)
        main_key, mesh_key = pass_fields_for_device(device, values)
        return build_local_image(
            args=args,
            config=config,
            device=device,
            version=version,
            packages=asu_packages + lime_packages,
            repositories=request["repositories"],
            main_key=main_key,
            mesh_key=mesh_key,
        )

    url = f"{config['asu_url']}/api/v1/build"
    print(f"Selector stable release: {version}")
    print(f"Device: {args.device} ({target}/{device['profile']})")
    print(f"LibreMesh feed: {args.feed_branch}")
    print(f"Extra packages: {' '.join(read_extra_packages(args)) or '(none)'}")
    print("Requesting a custom image from the Firmware Selector ASU service...")
    status, response = http_json(url, request, timeout=120)
    deadline = time.monotonic() + args.build_timeout
    while status == 202:
        request_hash = response.get("request_hash")
        if not request_hash:
            raise DeployError(f"ASU queued a build without a request hash: {response}")
        print(
            f"Build queued ({response.get('imagebuilder_status', 'waiting')}); "
            f"checking again in 10 seconds."
        )
        if time.monotonic() >= deadline:
            raise DeployError("Timed out waiting for the Selector build")
        time.sleep(10)
        status, response = http_json(
            f"{config['asu_url']}/api/v1/build/{urllib.parse.quote(request_hash)}",
            timeout=120,
        )
    if status != 200:
        detail = response.get("detail") or response.get("stderr") or response
        raise DeployError(f"ASU build failed (HTTP {status}): {str(detail)[:2500]}")

    images = response.get("images", [])
    image_type = getattr(args, "image_type", "sysupgrade")
    candidates = [image for image in images if image.get("type") == image_type]
    if image_type != "sysupgrade" and len(candidates) > 1:
        candidates = [i for i in candidates if "squashfs" in i.get("name", "")] or candidates
    if len(candidates) != 1:
        offered = ", ".join(f"{i.get('type')}:{i.get('name')}" for i in images)
        raise DeployError(
            f"ASU returned {len(candidates)} '{image_type}' images; expected exactly one. "
            f"Offered: {offered}"
        )
    image = candidates[0]
    image_name = image.get("name", "")
    expected_hash = image.get("sha256", "")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", image_name) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", expected_hash
    ):
        raise DeployError("ASU returned an invalid image name or SHA-256 checksum")
    bin_dir = str(response.get("bin_dir", "")).strip("/")
    if not bin_dir or ".." in bin_dir.split("/"):
        raise DeployError("ASU returned an invalid image directory")
    image_url = (
        f"{config['asu_url']}/store/{urllib.parse.quote(bin_dir, safe='/')}/"
        f"{urllib.parse.quote(image_name)}"
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / image_name
    temporary = output.with_suffix(output.suffix + ".part")
    print(f"Downloading {image_name}...")
    request_obj = urllib.request.Request(image_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request_obj, timeout=180) as source, temporary.open("wb") as target_file:
            shutil.copyfileobj(source, target_file)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        temporary.unlink(missing_ok=True)
        raise DeployError(f"Could not download custom firmware: {error}") from error
    actual_hash = hashlib.sha256(temporary.read_bytes()).hexdigest()
    if actual_hash.lower() != expected_hash.lower():
        temporary.unlink(missing_ok=True)
        raise DeployError(
            f"Image checksum mismatch: expected {expected_hash}, received {actual_hash}"
        )
    temporary.replace(output)
    metadata = {
        "device": args.device,
        "board": device["board"],
        "profile": device["profile"],
        "target": target,
        "openwrt_version": version,
        "libremesh_feed": args.feed_branch,
        "extra_packages": read_extra_packages(args),
        "image": image_name,
        "sha256": actual_hash,
        "asu_url": config["asu_url"],
    }
    (args.output_dir / f"{image_name}.build.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Verified image: {output}")
    print(f"SHA-256: {actual_hash}")
    return output


def create_failsafe_archive(failsafe_dir: Path) -> Path:
    config_dir = failsafe_dir.expanduser().resolve() / "etc" / "config"
    if not config_dir.is_dir():
        raise DeployError(f"Failsafe config directory missing: {config_dir}")
    for required in ("network", "wireless", "lime-node", "firewall", "dhcp"):
        if not (config_dir / required).is_file():
            raise DeployError(f"Failsafe backup is missing etc/config/{required}")
    handle, archive_name = tempfile.mkstemp(prefix="picot0-config-", suffix=".tar.gz")
    os.close(handle)
    archive_path = Path(archive_name)
    with tarfile.open(archive_path, "w:gz") as archive:
        paths = [config_dir] + sorted(config_dir.iterdir())
        for path in paths:
            if path.is_symlink() or (path != config_dir and not path.is_file()):
                raise DeployError(f"Unexpected non-regular failsafe entry: {path}")
            info = archive.gettarinfo(str(path), arcname=f"etc/config/{path.name}" if path != config_dir else "etc/config")
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            if path.is_file():
                with path.open("rb") as source:
                    archive.addfile(info, source)
            else:
                archive.addfile(info)
    return archive_path


def run_command(
    command: list[str], *, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command, check=False, capture_output=capture, text=capture
        )
    except FileNotFoundError as error:
        raise DeployError(f"Required command not found: {command[0]}") from error
    if check and result.returncode:
        raise DeployError(
            f"Command failed with exit status {result.returncode}: "
            f"{shlex.join(command[:4])}"
        )
    return result


def ssh(host: str, remote_command: str) -> None:
    run_command([
        *SSH_CMD,
        host, remote_command,
    ])


REMOTE_BOARD_CMD = (
    "board=$(ubus call system board 2>/dev/null | "
    "jsonfilter -e '@.board_name' 2>/dev/null || true); "
    "[ -n \"$board\" ] || board=$(cat /tmp/sysinfo/board_name 2>/dev/null || true); "
)


def check_remote_board(host: str, device: dict) -> str:
    wanted = normalize_board(device["board"])
    result = run_command(
        [
            *SSH_CMD,
            host, "set -eu; " + REMOTE_BOARD_CMD + "printf '%s' \"$board\"",
        ],
        capture=True,
    )
    board = result.stdout.strip()
    if normalize_board(board) != wanted:
        raise DeployError(
            f"Wrong board on {host}: device reports {board or 'nothing'!r}, "
            f"expected {device['board']!r}"
        )
    return board


def read_vlan_enabled(path: Path) -> bool:
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as error:
        raise DeployError(f"Could not read VLAN config: {path}") from error
    settings = re.findall(
        r"^\s*option\s+enabled\s+(['\"])([01])\1\s*(?:#.*)?$",
        contents,
        re.MULTILINE,
    )
    if len(settings) != 1:
        raise DeployError("VLAN config must set option enabled to 0 or 1 exactly once")
    return settings[0][1] == "1"


def read_equipment_pass(path: Path, require_vlan_keys: bool = True) -> dict[str, str]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise DeployError(
            f"Private password file not found: {path}. "
            f"Copy {ROOT / '.pass.example'} to {DEFAULT_PASS_FILE} and fill it in."
        )
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise DeployError(
            f"Password file must be private; run chmod 600 {path}"
        )
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.rstrip("\r")
        if not line or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            raise DeployError(f"Invalid .pass entry on line {line_number}; expected NAME=VALUE")
        name, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in values:
            raise DeployError(f"Invalid or duplicate .pass entry name on line {line_number}")
        values[name] = value
    for name in ("IOT_WIFI_KEY", "GUEST_WIFI_KEY"):
        value = values.get(name, "")
        if not value and not require_vlan_keys:
            continue
        if not 8 <= len(value) <= 63 or not value.isascii() or any(
            ord(char) < 32 or ord(char) > 126 for char in value
        ):
            raise DeployError(f"{name} in .pass must be 8-63 printable ASCII characters")
    return values


def pass_fields_for_device(device: dict, values: dict[str, str]) -> tuple[str, str]:
    if device["profile"] == "aruba_ap-303":
        main_name, mesh_name = "ARUBA_MAIN_WIFI_KEY", "ARUBA_MESH_KEY"
    elif device["profile"] == "xiaomi_redmi-router-ax6000-ubootmod":
        main_name, mesh_name = "PICOT0_MAIN_WIFI_KEY", "PICOT0_MESH_KEY"
    else:
        raise DeployError(
            "--inject-pass currently supports aruba-ap-303 and "
            "redmi-ax6000-ubootmod only"
        )
    result = []
    for name in (main_name, mesh_name):
        value = values.get(name, "")
        if not 8 <= len(value) <= 63 or not value.isascii() or any(
            ord(char) < 32 or ord(char) > 126 for char in value
        ):
            raise DeployError(f"{name} in .pass must be 8-63 printable ASCII characters")
        result.append(value)
    return result[0], result[1]


def build_lime_wifi_defaults(main_key: str, mesh_key: str) -> str:
    settings = {
        "ap_ssid": "villapicot",
        "ap_key": main_key,
        "ap_encryption": "psk2",
        "apname_ssid": "villapicot",
        "apname_key": main_key,
        "apname_encryption": "psk2",
        "ieee80211s_mesh_id": "picotMESH",
        "ieee80211s_encryption": "psk2+aes",
        "ieee80211s_key": mesh_key,
    }
    lines = [
        "#!/bin/sh",
        "set -eu",
        "uci set lime-node.wifi=lime",
        "uci -q delete lime-node.wifi.modes || true",
        "uci add_list lime-node.wifi.modes=ap",
        "uci add_list lime-node.wifi.modes=apname",
        "uci add_list lime-node.wifi.modes=ieee80211s",
    ]
    lines.extend(
        f"uci set {shlex.quote(f'lime-node.wifi.{name}={value}')}"
        for name, value in settings.items()
    )
    lines.extend(("uci commit lime-node", "exit 0", ""))
    return "\n".join(lines)


def local_imagebuilder_dir(
    args: argparse.Namespace, config: dict, version: str, target: str
) -> tuple[Path, str]:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise DeployError(
            "Local credential injection requires the Linux x86_64 OpenWrt ImageBuilder"
        )
    if args.imagebuilder_dir:
        builder = args.imagebuilder_dir.expanduser().resolve()
        if not (builder / "Makefile").is_file():
            raise DeployError(f"ImageBuilder Makefile not found in {builder}")
        version_file = builder / "include" / "version.mk"
        if not version_file.is_file() or not re.search(
            rf"^VERSION_NUMBER:=[^\n]*{re.escape(version)}",
            version_file.read_text(encoding="utf-8"),
            re.MULTILINE,
        ):
            raise DeployError(
                f"ImageBuilder at {builder} is not for OpenWrt {version}"
            )
        return builder, ""

    target_name, subtarget = target.split("/", 1)
    archive_name = (
        f"openwrt-imagebuilder-{version}-{target_name}-{subtarget}."
        "Linux-x86_64.tar.zst"
    )
    base_url = (
        f"{config['image_url'].rstrip('/')}/releases/{version}/targets/"
        f"{target_name}/{subtarget}/"
    )
    archive_url = urllib.parse.urljoin(base_url, archive_name)
    cache_root = Path.home() / ".cache" / "libremesh-deploy" / "imagebuilder"
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    builder = cache_root / archive_name.removesuffix(".tar.zst")
    if (builder / "Makefile").is_file():
        return builder, archive_url

    manifest_request = urllib.request.Request(
        urllib.parse.urljoin(base_url, "sha256sums"),
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(manifest_request, timeout=60) as response:
            manifest = response.read().decode("ascii")
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeError) as error:
        raise DeployError(f"Could not retrieve ImageBuilder checksums: {error}") from error
    expected_hash = None
    for line in manifest.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if match and Path(match.group(2).lstrip("*")).name == archive_name:
            expected_hash = match.group(1).lower()
            break
    if not expected_hash:
        raise DeployError(f"No SHA-256 entry for {archive_name}")

    archive_path = cache_root / archive_name
    temporary_archive = archive_path.with_suffix(archive_path.suffix + ".part")
    archive_request = urllib.request.Request(
        archive_url, headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(archive_request, timeout=180) as response:
            with temporary_archive.open("wb") as output:
                shutil.copyfileobj(response, output)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        temporary_archive.unlink(missing_ok=True)
        raise DeployError(f"Could not download ImageBuilder: {error}") from error
    actual_hash = hashlib.sha256(temporary_archive.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        temporary_archive.unlink(missing_ok=True)
        raise DeployError(
            f"ImageBuilder checksum mismatch: expected {expected_hash}, received {actual_hash}"
        )
    temporary_archive.replace(archive_path)

    try:
        with tempfile.TemporaryDirectory(
            prefix="libremesh-imagebuilder-", dir=cache_root
        ) as extraction_dir:
            result = subprocess.run(
                ["tar", "--zstd", "-xf", str(archive_path), "-C", extraction_dir],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                raise DeployError(
                    "Could not extract ImageBuilder: "
                    + (result.stderr.strip() or f"tar exited {result.returncode}")
                )
            roots = [
                path for path in Path(extraction_dir).iterdir()
                if path.is_dir() and (path / "Makefile").is_file()
            ]
            if len(roots) != 1:
                raise DeployError("ImageBuilder archive did not contain one build tree")
            shutil.move(str(roots[0]), str(builder))
    except FileExistsError as error:
        raise DeployError(f"ImageBuilder cache path already exists: {builder}") from error
    return builder, archive_url


def build_local_image(
    args: argparse.Namespace,
    config: dict,
    device: dict,
    version: str,
    packages: list[str],
    repositories: dict[str, str],
    main_key: str,
    mesh_key: str,
) -> Path:
    if not shutil.which("gawk"):
        raise DeployError(
            "Local ImageBuilder requires GNU awk; install the gawk package and retry"
        )
    builder, imagebuilder_url = local_imagebuilder_dir(
        args, config, version, device["target"]
    )
    repo_file = builder / "repositories"
    if not repo_file.is_file():
        raise DeployError(f"ImageBuilder repositories file not found: {repo_file}")
    repo_lines = repo_file.read_text(encoding="utf-8").splitlines()
    for url in repositories.values():
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "downloads.openwrt.org",
            "feed.libremesh.org",
        }:
            raise DeployError(f"Unexpected package repository URL: {url}")
        if url not in repo_lines:
            repo_lines.append(url)
    repo_file.write_text("\n".join(repo_lines) + "\n", encoding="utf-8")

    keys_dir = builder / "keys"
    keys_dir.mkdir(exist_ok=True)
    saved_key = False
    for index, key in enumerate(config.get("repository_keys", [])):
        if not isinstance(key, str):
            continue
        key = key.strip()
        if key.startswith("-----BEGIN PUBLIC KEY-----"):
            (keys_dir / f"libremesh-feed-{index}.pem").write_text(
                key + "\n", encoding="ascii"
            )
            saved_key = True
        elif re.fullmatch(r"[A-Za-z0-9+/=]+", key):
            (keys_dir / f"libremesh-feed-{index}.pub").write_text(
                key + "\n", encoding="ascii"
            )
            saved_key = True
    if not saved_key:
        raise DeployError("Selector did not provide a package feed signing key")

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="libremesh-pass-files-") as files_dir:
        defaults_dir = Path(files_dir) / "etc" / "uci-defaults"
        defaults_dir.mkdir(parents=True)
        defaults_script = defaults_dir / "90zz-lime-wifi"
        defaults_script.write_text(
            build_lime_wifi_defaults(main_key, mesh_key), encoding="ascii"
        )
        defaults_script.chmod(0o755)
        command = [
            "make",
            "-j2",
            "image",
            f"PROFILE={device['profile']}",
            f"PACKAGES={' '.join(unique(packages))}",
            f"FILES={files_dir}",
        ]
        print("Building locally with the matching OpenWrt ImageBuilder.")
        print("Wi-Fi and mesh credentials stay local; do not share the resulting image.")
        try:
            result = subprocess.run(
                command,
                cwd=builder,
                capture_output=True,
                text=True,
                timeout=args.build_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise DeployError("Local ImageBuilder timed out") from error
        if result.returncode:
            details = result.stdout + "\n" + result.stderr
            for secret in (main_key, mesh_key):
                if secret:
                    details = details.replace(secret, "[redacted]")
            raise DeployError(
                "Local ImageBuilder failed: "
                + details.strip()[-3000:]
            )

    target_name, subtarget = device["target"].split("/", 1)
    image_dir = builder / "bin" / "targets" / target_name / subtarget
    candidates = sorted(image_dir.glob(f"*{device['profile']}*sysupgrade.bin"))
    if len(candidates) != 1:
        raise DeployError(
            f"Expected one local sysupgrade image for {device['profile']}, "
            f"found {len(candidates)} in {image_dir}"
        )
    image_name = candidates[0].name
    output = output_dir / f"{image_name.removesuffix('.bin')}-with-pass.bin"
    temporary = output.with_suffix(output.suffix + ".part")
    shutil.copyfile(candidates[0], temporary)
    temporary.chmod(0o600)
    temporary.replace(output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    metadata = {
        "device": args.device,
        "board": device["board"],
        "profile": device["profile"],
        "target": device["target"],
        "openwrt_version": version,
        "libremesh_feed": args.feed_branch,
        "extra_packages": read_extra_packages(args),
        "image": output.name,
        "sha256": digest,
        "build_method": "local ImageBuilder",
        "credentials_embedded": True,
        "imagebuilder_url": imagebuilder_url,
    }
    metadata_path = output_dir / f"{output.name}.build.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    metadata_path.chmod(0o600)
    print(f"Built local image with first-boot LibreMesh Wi-Fi settings: {output}")
    print(f"SHA-256: {digest}")
    print("The image contains readable network credentials; keep it private.")
    return output


def configure_vlans(args: argparse.Namespace) -> None:
    device = resolve_device(args.device, getattr(args, "target", None))
    disable_config: Path | None = None
    if args.disable:
        if args.config:
            raise DeployError("--disable and --config cannot be combined")
        handle, name = tempfile.mkstemp(prefix="libremesh-vlan-off-", suffix=".conf")
        with os.fdopen(handle, "w", encoding="ascii") as output:
            output.write("config vlan 'settings'\n\toption enabled '0'\n\toption role 'edge'\n")
        disable_config = Path(name)
        config_path = disable_config
        pass_values: dict[str, str] = {}
    else:
        if not args.config:
            raise DeployError("--config is required unless --disable is used")
        config_path = args.config.expanduser().resolve()
        if not config_path.is_file():
            raise DeployError(f"VLAN config file not found: {config_path}")
        pass_values = read_equipment_pass(
            args.pass_file, require_vlan_keys=read_vlan_enabled(config_path)
        )
    try:
        _configure_vlans(args, device, config_path, pass_values)
    finally:
        if disable_config:
            disable_config.unlink(missing_ok=True)


def _configure_vlans(
    args: argparse.Namespace, device: dict, config_path: Path, pass_values: dict[str, str]
) -> None:
    if not VLAN_SCRIPT.is_file():
        raise DeployError(f"VLAN apply script not found: {VLAN_SCRIPT}")
    if not VLAN_RECOVERY_SCRIPT.is_file():
        raise DeployError(f"VLAN boot recovery script not found: {VLAN_RECOVERY_SCRIPT}")

    remote_dir = "/root/libremesh-failsafe"
    remote_script = f"{remote_dir}/apply-libremesh-vlans.sh"
    remote_config = f"{remote_dir}/picot-vlan.conf.upload"
    remote_pass = f"{remote_dir}/equipment.pass.upload"

    def cleanup_remote_uploads() -> str:
        cleanup = run_command(
            [
                *SSH_CMD,
                args.host,
                f"rm -f {remote_config} {remote_pass} "
                "/tmp/apply-libremesh-vlans.sh /tmp/picot-vlan-recovery",
            ],
            check=False,
        )
        return (
            ""
            if cleanup.returncode == 0
            else " Could not clean up temporary VLAN files; remove them after reconnecting."
        )

    print(f"Checking SSH access and board identity on {args.host}...")
    check_remote_board(args.host, device)
    try:
        ssh(args.host, f"mkdir -p {remote_dir}")
        run_command([
            *SCP_CMD,
            str(VLAN_SCRIPT), f"{args.host}:/tmp/apply-libremesh-vlans.sh",
        ])
        run_command([
            *SCP_CMD,
            str(config_path), f"{args.host}:{remote_config}",
        ])
        pass_fd, pass_name = tempfile.mkstemp(prefix="libremesh-vlan-", suffix=".pass")
        pass_bundle = Path(pass_name)
        try:
            os.fchmod(pass_fd, 0o600)
            with os.fdopen(pass_fd, "w", encoding="ascii") as output:
                output.write(f"IOT_WIFI_KEY={pass_values.get('IOT_WIFI_KEY', '')}\n")
                output.write(f"GUEST_WIFI_KEY={pass_values.get('GUEST_WIFI_KEY', '')}\n")
            run_command([
                *SCP_CMD,
                str(pass_bundle), f"{args.host}:{remote_pass}",
            ])
        finally:
            pass_bundle.unlink(missing_ok=True)
        run_command([
            *SCP_CMD,
            str(VLAN_RECOVERY_SCRIPT), f"{args.host}:/tmp/picot-vlan-recovery",
        ])
        ssh(
            args.host,
            "set -eu; "
            f"mkdir -p {remote_dir}; "
            f"chmod 600 {remote_config}; "
            f"chmod 600 {remote_pass}; "
            f"cp /tmp/apply-libremesh-vlans.sh {remote_script}; "
            f"chmod 700 {remote_script}; "
            "rm -f /tmp/apply-libremesh-vlans.sh; "
            f"{remote_script} validate {remote_config} {remote_pass}",
        )
    except DeployError as error:
        raise DeployError(f"{error}.{cleanup_remote_uploads()}") from error
    if args.validate_only:
        ssh(
            args.host,
            f"rm -f {remote_config} {remote_pass} /tmp/picot-vlan-recovery",
        )
        print("VLAN config validated; no network changes made.")
        return

    try:
        ssh(
            args.host,
            "set -eu; "
            "cp /tmp/picot-vlan-recovery /etc/init.d/picot-vlan-recovery; "
            "chmod 755 /etc/init.d/picot-vlan-recovery; "
            "rm -f /tmp/picot-vlan-recovery; "
            "/etc/init.d/picot-vlan-recovery enable",
        )
    except DeployError as error:
        raise DeployError(f"{error}.{cleanup_remote_uploads()}") from error
    print("Applying VLANs with a 180-second automatic rollback window...")
    try:
        ssh(args.host, f"{remote_script} apply {remote_config} {remote_pass}")
    except DeployError as error:
        raise DeployError(
            f"{error}. The connection may have dropped during network reload; "
            "do not retry yet. Reconnect and confirm the change, or wait for "
            f"the 180-second automatic rollback.{cleanup_remote_uploads()}"
        ) from error


def manage_autowan(args: argparse.Namespace) -> None:
    device = resolve_device(args.device, None)
    check_remote_board(args.host, device)
    if args.action == "status":
        ssh(args.host, "test -x /usr/sbin/picot-autowan && /usr/sbin/picot-autowan status")
        return
    if args.action == "disable":
        ssh(
            args.host,
            "set -eu; test -x /usr/sbin/picot-autowan; "
            "/usr/sbin/picot-autowan configure disable; "
            "/etc/init.d/picot-autowan stop || true; "
            "/etc/init.d/picot-autowan disable || true; "
            "/usr/sbin/picot-autowan demote-all",
        )
        print("Automatic WAN detection disabled; promoted ports returned to the LAN bridge.")
        return
    for path in (AUTOWAN_SCRIPT, AUTOWAN_INIT):
        if not path.is_file():
            raise DeployError(f"Missing file: {path}")
    for path, remote in ((AUTOWAN_SCRIPT, "/tmp/picot-autowan"), (AUTOWAN_INIT, "/tmp/picot-autowan.init")):
        run_command([
            *SCP_CMD,
            str(path), f"{args.host}:{remote}",
        ])
    ssh(
        args.host,
        "set -eu; "
        "cp /tmp/picot-autowan /usr/sbin/picot-autowan; "
        "chmod 755 /usr/sbin/picot-autowan; "
        "cp /tmp/picot-autowan.init /etc/init.d/picot-autowan; "
        "chmod 755 /etc/init.d/picot-autowan; "
        "rm -f /tmp/picot-autowan /tmp/picot-autowan.init; "
        "/usr/sbin/picot-autowan check; "
        "/usr/sbin/picot-autowan configure enable; "
        "for p in /usr/sbin/picot-autowan /etc/init.d/picot-autowan "
        "/etc/config/picot-autowan; do "
        "grep -qxF \"$p\" /etc/sysupgrade.conf 2>/dev/null || "
        "echo \"$p\" >> /etc/sysupgrade.conf; done; "
        "/etc/init.d/picot-autowan enable; "
        "/etc/init.d/picot-autowan restart",
    )
    print("Automatic WAN detection is running. Check with: autowan --action status")


def finish_vlan_change(args: argparse.Namespace, action: str) -> None:
    device = resolve_device(args.device, getattr(args, "target", None))
    if not re.fullmatch(r"\d{14}-\d+", args.change_id):
        raise DeployError("Invalid VLAN change ID")
    check_remote_board(args.host, device)
    remote_script = "/root/libremesh-failsafe/apply-libremesh-vlans.sh"
    ssh(
        args.host,
        f"test -x {remote_script} && "
        f"{remote_script} {action} {shlex.quote(args.change_id)}",
    )


def stage_image(args: argparse.Namespace, image_path: Path) -> None:
    device = resolve_device(args.device, getattr(args, "target", None))
    image_path = image_path.expanduser().resolve()
    if not image_path.is_file():
        raise DeployError(f"Firmware image not found: {image_path}")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", image_path.name):
        raise DeployError("Firmware image filename contains unsupported characters")
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    remote_dir = "/root/libremesh-failsafe"
    remote_image = f"/tmp/{image_path.name}"
    backup_path: Path | None = None
    try:
        if getattr(args, "failsafe_dir", None):
            backup_path = create_failsafe_archive(args.failsafe_dir)
        print(f"Checking SSH access and board identity on {args.host}...")
        remote_board = check_remote_board(args.host, device)

        print("Copying firmware and rollback helper...")
        run_command([
            *SCP_CMD,
            str(image_path), f"{args.host}:{remote_image}",
        ])
        run_command([
            *SCP_CMD,
            str(ROOT / "restore-libremesh-config.sh"),
            f"{args.host}:/tmp/restore-libremesh-config.sh",
        ])
        if backup_path:
            run_command([
                *SCP_CMD,
                str(backup_path), f"{args.host}:/tmp/picot0-config.tar.gz",
            ])

        setup = (
            "set -eu; "
            f"mkdir -p {remote_dir}; "
            "tar -czf /root/libremesh-failsafe/live-config.tar.gz -C / etc/config; "
            "cp /tmp/restore-libremesh-config.sh "
            f"{remote_dir}/restore-libremesh-config.sh; "
            f"chmod 700 {remote_dir}/restore-libremesh-config.sh; "
            "rm -f /tmp/restore-libremesh-config.sh; "
            + (
                "cp /tmp/picot0-config.tar.gz "
                f"{remote_dir}/picot0-config.tar.gz; "
                f"chmod 600 {remote_dir}/picot0-config.tar.gz; "
                "rm -f /tmp/picot0-config.tar.gz; "
                if backup_path
                else ""
            )
            + f"for p in {remote_dir}/live-config.tar.gz "
            f"{remote_dir}/restore-libremesh-config.sh "
            + (
                f"{remote_dir}/picot0-config.tar.gz "
                if backup_path
                else ""
            )
            + "; do grep -qxF \"$p\" /etc/sysupgrade.conf 2>/dev/null "
            "|| echo \"$p\" >> /etc/sysupgrade.conf; done; "
            f"actual=$(sha256sum {shlex.quote(remote_image)} | awk '{{print $1}}'); "
            f"[ \"$actual\" = {shlex.quote(digest)} ] "
            "|| { echo 'Uploaded firmware checksum mismatch' >&2; exit 22; }; "
            f"sysupgrade -T {shlex.quote(remote_image)}"
        )
        ssh(args.host, setup)
        print()
        print(f"Image and rollback files are staged on {args.host}.")
        print("The current config has been backed up and added to sysupgrade.conf.")
        if backup_path:
            print(
                "The saved failsafe config is installed at "
                f"{remote_dir}/picot0-config.tar.gz."
            )
        print(f"Firmware SHA-256: {digest}")
        if args.flash:
            if not sys.stdin.isatty():
                raise DeployError("--flash requires an interactive terminal")
            confirmation = f"FLASH {remote_board}"
            print(f"This will reboot the device and install {image_path.name}.")
            print(f"Type exactly '{confirmation}' to continue:")
            if input("> ").strip() != confirmation:
                raise DeployError("Flash cancelled; staged files were left in place")
            remote_flash = (
                "nohup sh -c "
                + shlex.quote(f"sleep 2; exec sysupgrade -c -v {remote_image}")
                + " >/tmp/libremesh-sysupgrade.log 2>&1 </dev/null &"
            )
            ssh(args.host, remote_flash)
            print("Upgrade started. Reconnect after the device reboots.")
    finally:
        if backup_path:
            backup_path.unlink(missing_ok=True)


def add_device_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--device",
        required=True,
        metavar="DEVICE",
        help="OpenWrt board name (e.g. aruba,ap-303), Selector profile id "
        "(aruba_ap-303), or shortcut: " + ", ".join(sorted(DEVICES)),
    )


def add_failsafe_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--failsafe-dir",
        type=Path,
        help="Optional known-good backup directory containing etc/config "
        "(for example backup-PICOT0-2026-10-30); installed on the device as "
        "the 'saved' restore source",
    )


def add_package_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--extra-packages",
        action="append",
        default=[],
        metavar="LIST",
        help="Additional package names (repeatable; quote space-separated lists)",
    )
    parser.add_argument(
        "--no-default-extra-packages",
        action="store_true",
        help="Do not include packages from firmware-selector-packages.txt",
    )


def add_build_options(parser: argparse.ArgumentParser) -> None:
    add_device_option(parser)
    parser.add_argument(
        "--inject-pass",
        action="store_true",
        help="Embed the Aruba/PICOT0 Wi-Fi and mesh keys from the private .pass "
        "using a local ImageBuilder; secrets are not sent to ASU",
    )
    parser.add_argument(
        "--pass-file",
        type=Path,
        default=DEFAULT_PASS_FILE,
        help="Private credential file for --inject-pass (default: .pass)",
    )
    parser.add_argument(
        "--imagebuilder-dir",
        type=Path,
        help="Use an already extracted, matching ImageBuilder instead of downloading it",
    )
    parser.add_argument(
        "--target",
        help="Build target such as ipq40xx/generic; only needed if the profile "
        "exists for several targets",
    )
    parser.add_argument(
        "--version",
        default="latest",
        help="OpenWrt version, or 'latest' for the Selector's current stable release",
    )
    parser.add_argument("--feed-branch", default="master", help="LibreMesh feed branch")
    parser.add_argument(
        "--image-type",
        default="sysupgrade",
        help="ASU image type to download (default sysupgrade; x86 emulator "
        "builds use 'combined')",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "firmware-output",
        help="Directory for verified image and build metadata",
    )
    parser.add_argument(
        "--uci-defaults",
        help="Optional first-boot UCI-defaults script sent to the Selector build",
    )
    parser.add_argument(
        "--build-timeout",
        type=int,
        default=3600,
        help="Maximum wait for a queued Selector build in seconds",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve current Selector settings and packages without requesting a build",
    )
    add_package_options(parser)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and optionally stage current LibreMesh Firmware Selector images."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="Build and verify a custom image")
    add_build_options(build)

    stage = commands.add_parser("stage", help="Stage an existing image and failsafes")
    add_device_option(stage)
    stage.add_argument("--host", required=True, help="SSH host, for example root@PICOT0")
    stage.add_argument("--image", required=True, type=Path)
    add_failsafe_option(stage)
    stage.add_argument(
        "--flash",
        action="store_true",
        help="Prompt for exact board confirmation and start sysupgrade",
    )

    deploy = commands.add_parser("deploy", help="Build, stage, and optionally flash")
    add_build_options(deploy)
    deploy.add_argument("--host", required=True, help="SSH host, for example root@PICOT0")
    add_failsafe_option(deploy)
    deploy.add_argument(
        "--flash",
        action="store_true",
        help="Prompt for exact board confirmation and start sysupgrade",
    )

    vlans = commands.add_parser(
        "vlans",
        help="Validate, apply or disable (--disable) an optional per-node VLAN configuration",
    )
    add_device_option(vlans)
    vlans.add_argument("--host", required=True, help="SSH host, for example root@PICOT0")
    vlans.add_argument("--config", type=Path, help="Private per-node UCI config")
    vlans.add_argument(
        "--disable",
        action="store_true",
        help="Return this node to normal behaviour without VLANs (still meshes "
        "with VLAN nodes); same timed rollback/confirm flow as enabling",
    )
    vlans.add_argument(
        "--pass-file",
        type=Path,
        default=DEFAULT_PASS_FILE,
        help="Private equipment password file (default: .pass)",
    )
    vlans.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate and stage config without changing network settings",
    )

    vlan_confirm = commands.add_parser(
        "vlan-confirm", help="Confirm a VLAN change before automatic rollback"
    )
    add_device_option(vlan_confirm)
    vlan_confirm.add_argument("--host", required=True)
    vlan_confirm.add_argument("--change-id", required=True)

    vlan_rollback = commands.add_parser(
        "vlan-rollback", help="Immediately roll back a pending VLAN change"
    )
    add_device_option(vlan_rollback)
    vlan_rollback.add_argument("--host", required=True)
    vlan_rollback.add_argument("--change-id", required=True)

    autowan = commands.add_parser(
        "autowan",
        help="Install/enable, disable or inspect automatic LAN-port WAN detection",
    )
    add_device_option(autowan)
    autowan.add_argument("--host", required=True)
    autowan.add_argument(
        "--action", choices=["enable", "disable", "status"], default="enable"
    )

    args = parser.parse_args()
    try:
        if (
            args.command == "deploy"
            and getattr(args, "inject_pass", False)
            and getattr(args, "flash", False)
        ):
            raise DeployError(
                "--inject-pass images require a fresh first boot, but deploy --flash "
                "preserves the current config. Build and stage the image, then install "
                "it with config reset using local recovery access."
            )
        if args.command == "build":
            build_image(args)
        elif args.command == "stage":
            stage_image(args, args.image)
        elif args.command == "deploy":
            image = build_image(args)
            if image is not None:
                stage_image(args, image)
        elif args.command == "vlans":
            configure_vlans(args)
        elif args.command == "autowan":
            manage_autowan(args)
        elif args.command == "vlan-confirm":
            finish_vlan_change(args, "confirm")
        else:
            finish_vlan_change(args, "rollback")
        return 0
    except (DeployError, OSError, tarfile.TarError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
