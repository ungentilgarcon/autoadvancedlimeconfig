# PICOT LibreMesh configuration and deployment

This directory is the working collection for the PICOT LibreMesh network. It
contains existing device configuration backups, firmware images and notes,
plus a script for requesting current customized LibreMesh firmware and staging
it on a device with a configuration rollback available.

> **VLAN support is optional and is not active on the devices yet.** The
> `vlans` command can apply it to one explicitly selected node at a time, with
> an automatic rollback timer. Existing nodes and the main network remain
> unchanged unless you apply a VLAN config to that node.

## Start here

1. Read this file before changing a node.
2. Keep this directory private. The configuration backups contain Wi-Fi
   credentials and other device settings.
3. Use `--dry-run` to review the current Selector release, device profile, and
   package list.
4. Build and stage first **without** `--flash`. Confirm the correct device and
   that its rollback files are present before considering a firmware upgrade.
5. Test changes on one isolated/pilot node before rolling them across the
   mesh.

## What is in this directory

| File or directory | Purpose |
|---|---|
| `deploy-libremesh.py` | Requests Selector/ASU images, verifies SHA-256, checks device board identity, stages rollback files, tests image compatibility, and optionally starts sysupgrade. |
| `restore-libremesh-config.sh` | On-device interactive restore for the saved PICOT0 config or the device's captured pre-upgrade config. |
| `apply-libremesh-vlans.sh` | Applies or removes managed VLAN UCI sections on a device with a timed rollback and explicit confirmation. |
| `picot-vlan-recovery` | Boot-time recovery hook; restores a pending transaction before network startup if the node reboots. |
| `.pass.example` | Credential-file template; private `.pass` is ignored by Git and supplies VLAN SSID keys. |
| `vlan-configs/*.example` | Inactive UCI templates. Set per-node role/radios/SSIDs here; Wi-Fi keys stay in `.pass`. |
| `firmware-selector-packages.txt` | Extra OpenWrt packages to request by default. |
| `backup-PICOT0-2026-10-30/` | Saved PICOT0 `/etc` backup, including `etc/config`. It is the source of the PICOT0-specific rollback archive. |
| `backup-PICOT0-2026-10-30.tar.gz` | Compressed copy of the PICOT0 backup. |
| `backup-LiMe-XIAOMI-2026-07-25.tar.gz` | Older Xiaomi configuration backup. Its settings differ from current PICOT0; do not restore it wholesale. |
| `script_config_lime_AP*.txt` | Previous setup-script experiments. They are references, **not safe to run as-is**. |
| `FWrulesV0.txt`, `propagateFWrules.txt` | Firewall experiments. They assume networks/zones that are not configured by the deployment script; do not run as-is. |
| `openwrt-*.bin`, `openwrt-*.itb` | Previously saved firmware images. These may be stale, customized, or for a different hardware variant. The deployment script does not use them unless explicitly given to `stage`. |
| `DEPLOYMENT.txt` | Short deployment notes; this README is the fuller guide. |

Protect these backups as secrets. Do not paste them into chat, publish them,
or commit them to a public repository. If a backed-up Wi-Fi key is still in
use and has been exposed, rotate it on all affected nodes.

## Current network snapshot

The following describes the PICOT0 configuration captured on 2026-10-30 and
the read-only information provided for the active node. Check the device
before relying on these values; they can change.

### Devices and firmware profiles

| Device | Board identifier | Selector profile | OpenWrt target |
|---|---|---|---|
| Xiaomi Redmi Router AX6000, U-Boot layout | `xiaomi,redmi-router-ax6000-ubootmod` | `xiaomi_redmi-router-ax6000-ubootmod` | `mediatek/filogic` |
| Aruba AP-303 | `aruba,ap-303` | `aruba_ap-303` | `ipq40xx/generic` |

The Xiaomi **Redmi AX6000 U-Boot layout** is not the same target as the
Qualcomm/IPQ50xx Xiaomi AX6000 image or the MediaTek non-U-BootMod variant.
Likewise, Aruba AP-303 and AP-303H are separate profiles. The script checks
the board identifier before staging an image to help prevent cross-flashing.

**The tool is not limited to these two models, and no model has a special
role** (any device with an uplink can be a WAN node). `--device` accepts:

- an OpenWrt board name, as shown by `ubus call system board` (`aruba,ap-303`),
- a Firmware Selector profile id (`aruba_ap-303`), or
- the shortcuts `redmi-ax6000-ubootmod` and `aruba-ap-303`.

The build target (e.g. `ipq40xx/generic`) is discovered from the Selector; add
`--target` only if a profile exists for several targets. The board check on the
device compares names with `,` and `_` treated alike. The flash confirmation
uses the board name the device itself reports.

PICOT0 was reported as LibreMesh master revision `b88562f`, built on OpenWrt
25.12.5, with hostname `PICOT0`. The Aruba AP-303 was also reported on the
same LibreMesh revision, but its `ubus` binary was missing. The deployment
script falls back to `/tmp/sysinfo/board_name` for board identification.

### PICOT0 LAN, mesh, and WAN

- Main client SSID: `villapicot` on 2.4 GHz and 5 GHz.
- Mesh ID: `picotMESH`. This is the backhaul and is distinct from the client
  SSID.
- 2.4 GHz: channel 11, HT20.
- 5 GHz: channel 48, HT40.
- Main LibreMesh network: `10.100.0.0/16`; the captured PICOT0 address is
  `10.100.159.52/16`.
- The captured IPv6 LAN address is `fd64:de5a:8e09::e:9f34/64`.
- In the captured Redmi configuration, `br-lan` bridges `bat0` with `lan2`,
  `lan3`, and `lan4`; `wan` is a separate DHCP interface.
- LibreMesh also generated mesh transport interfaces on the mesh radios and
  `wan`. The captured batman-adv transport uses VLAN ID 116 and the Babel
  transport uses VLAN ID 17. These are **LibreMesh transport VLANs**, not the
  planned user VLANs 10/20/30; do not change them as part of client VLAN work.
- The observed WAN route output contained two default routes via
  `192.168.0.254`, one static and one DHCP. Confirm why both exist before
  configuring additional WANs or failover.
- The Aruba AP-303 has a single Ethernet interface (`lan@eth0`) in the
  observed device output. A port role that is possible on the multi-port
  Redmi may not be possible on an AP-303 without changing its only wired path.

LibreMesh builds parts of its network configuration from `/etc/config/lime-*`.
Generated `network` entries can contain per-device addresses, MAC-derived
values, or interface names. Do not copy the full generated `network` or
`wireless` config from one node/model to another. Use a node-specific
`lime-node` configuration and let LibreMesh generate interfaces, then verify
the result on that device.

## Optional VLAN design

The requested starting point is:

| VLAN | Purpose | Proposed client SSID | Addressing | Proposed policy |
|---:|---|---|---|---|
| — | Existing/main client network | `villapicot` | Preserve existing LibreMesh addressing | Remains untagged and unchanged |
| 10 | IoT devices | `villapicot-IOT` | `192.168.10.0/24` | No WAN forwarding; main may initiate management connections |
| 20 | Guests | `villapicot-GUEST` | `192.168.20.0/24` | WAN access; AP client isolation; main may initiate connections |

VLAN 10 and 20 are carried as batman-adv VLAN devices (`bat0.10` and
`bat0.20`) across the mesh. DHCP and routing are hosted on the one node whose
config has `role 'gateway'` (PICOT0 in the examples; any node can hold the
role, but use **exactly one gateway per mesh**, it is not checked across nodes). VLAN
clients on configured APs can therefore reach that gateway through intervening
legacy nodes without requiring those nodes to become VLAN-aware. The existing
`villapicot` SSID and untagged `br-lan` remain the common network across old
and new nodes; VLAN 30 is not created or assumed.

VLANs are added only to radios named in that node's config. All physical
Ethernet ports and their existing roles are left untouched and remain
untagged on the current main network. This first implementation does not
configure Ethernet access ports/trunks. WAN uplinks are handled separately by
automatic WAN detection (see below).

Firewall policy on PICOT0: IoT clients may use gateway DHCP/DNS but have no
WAN forwarding; Guest clients may use gateway DHCP/DNS and forward to WAN;
neither VLAN can initiate connections to the main LAN or the other VLAN. Main
LAN forwarding to either VLAN is permitted at PICOT0. Guest Wi-Fi client
isolation is enabled. These policies are applied only when the gateway config
is enabled on PICOT0. Main-to-VLAN reachability from clients using a legacy
node as their gateway depends on that node's routing; it is not guaranteed by
this VLAN change alone and must be verified before relying on it.

### Configure and apply one node

The templates in `vlan-configs/` are disabled and contain no Wi-Fi keys.
The private `.pass` file contains equipment credentials and is excluded from
Git. Current PICOT0 Wi-Fi/mesh keys were copied into it from the saved config;
the backups themselves remain unchanged. It has fields for other device
credentials and the new VLAN SSID keys; blank values must be filled locally.
The VLAN deployer requires
`IOT_WIFI_KEY` and `GUEST_WIFI_KEY`, each 8–63 printable ASCII characters.
Use the same value per SSID across APs if clients should roam. The router
admin password fields are not used by deployment; SSH uses public-key
authentication and strict host-key checking.

The local `.pass` file is already created. If it is missing, initialize it
from the template and restrict access:

```sh
cp .pass.example .pass
chmod 600 .pass
```

Edit `.pass` with a private editor; do not print it to the terminal or paste
it into chat. Per-node UCI files only contain non-secret names, roles, and
radio selections.

Example for PICOT0:

```sh
cp vlan-configs/PICOT0.example /tmp/PICOT0-vlan.conf
# Set enabled to 1; enter IOT_WIFI_KEY and GUEST_WIFI_KEY in .pass.
python3 deploy-libremesh.py vlans \
  --device redmi-ax6000-ubootmod \
  --host root@PICOT0 \
  --config /tmp/PICOT0-vlan.conf \
  --validate-only
```

After validation, run without `--validate-only` to apply:

```sh
python3 deploy-libremesh.py vlans \
  --device redmi-ax6000-ubootmod \
  --host root@PICOT0 \
  --config /tmp/PICOT0-vlan.conf
```

The command requires strict SSH host-key checking and verifies the board
identifier. The `gateway` role is not tied to a model or hostname: choose the
node that should serve VLAN DHCP/routing (with an uplink for guest Internet).
For each other VLAN-enabled AP, make an edge config from `vlan-configs/edge.example`, set the proper radios, then
use the matching device profile and SSH host.
Leave `enabled` at `0` on nodes that should continue operating as legacy-only
nodes. This `vlans` command configures the installed firmware directly; it
does not build or flash firmware. Only the two VLAN SSID keys are sent to the
device, in a root-only temporary file; the applied keys are kept in the
root-only `/root/libremesh-failsafe/equipment.pass`.

Example Aruba edge configuration:

```sh
cp vlan-configs/edge.example /tmp/ARUBA-vlan.conf
# Set enabled to 1 and verify the radio names.
python3 deploy-libremesh.py vlans \
  --device aruba-ap-303 \
  --host root@ARUBA_HOST \
  --config /tmp/ARUBA-vlan.conf \
  --validate-only
```

Applying changes backs up the current `network`, `wireless`, `dhcp`, and
`firewall` UCI files first, then starts a 180-second watchdog before reloading
services. A boot-time hook also restores a still-pending transaction before
network startup if the node reboots. The command prints a `VLAN_CHANGE_ID` only
after the new config has been applied. From a still-working SSH connection,
confirm promptly:

```sh
python3 deploy-libremesh.py vlan-confirm \
  --device redmi-ax6000-ubootmod \
  --host root@PICOT0 \
  --change-id CHANGE_ID
```

Replace `CHANGE_ID` with the printed value. If connectivity is broken or
confirmation does not arrive before the timer expires, the device restores the
saved UCI files automatically. To undo immediately while the change is still
pending:

```sh
python3 deploy-libremesh.py vlan-rollback \
  --device redmi-ax6000-ubootmod \
  --host root@PICOT0 \
  --change-id CHANGE_ID
```

Do not roll out to the fleet at once. First use PICOT0 and one reachable pilot
AP with local recovery available. Verify `bat0.10`/`bat0.20`, each new SSID,
DHCP, guest Internet access, IoT WAN isolation, and access from the existing
main network before confirming. The change is applied by direct UCI updates;
it does not invoke `lime-config`, which could regenerate LibreMesh-managed
network or wireless sections.

### Return a node to normal behaviour (no VLANs)

If VLANs do not work on a node, remove them from just that node:

```sh
python3 deploy-libremesh.py vlans --device aruba,ap-303 --host root@ARUBA_HOST --disable
```

This uses the same timed-rollback flow and prints a `VLAN_CHANGE_ID` to
confirm. It deletes only the `picot_vlan_*` sections (VLAN devices, VLAN
SSIDs, DHCP/firewall entries) and leaves the main untagged network, mesh and
Ethernet settings as they were. The node keeps interoperating with VLAN-enabled
nodes: batman-adv still forwards their tagged VLAN 10/20 frames transparently,
and the main network is untagged, so mixed VLAN and non-VLAN nodes stay
connected. Clients of the removed VLAN SSIDs simply lose those SSIDs on that
node. If an enabling change fails or is not confirmed, the automatic rollback
returns the node to its previous state, which is non-VLAN if it had none.

For rollout, enable PICOT0 first so it provides DHCP and routing, then enable
edge APs one at a time. To retire the VLANs, disable edge APs first and PICOT0
last. A node left with `enabled '0'` keeps only its existing untagged main
network and continues to interoperate with both legacy nodes and enabled nodes
on that main network.

## Automatic WAN detection on any LAN port

Goal: plug an Internet-connected cable into **any** Ethernet port of **any**
node and have it become the WAN uplink. Several nodes may each have an uplink.

### Does the existing Babel configuration work on the Arubas? Yes.

PICOT0's `lime-defaults`/`lime-autogen` enable **both** protocols on every mesh
interface: batman-adv (VLAN 116, layer-2 bridging into `br-lan`) and Babel
(VLAN 17, routing; `babeld` has filters that redistribute a default route
tagged `proto 7`). The Aruba AP-303 gets the same LibreMesh defaults, the same
firmware package set (including `babeld-auto-gw-mode` and `watchping`) and the
same Wi-Fi mesh, so it takes part in the same Babel network with no extra
configuration. Nothing needs to move to batman-adv.

Moving the gateway logic to batman-adv would in fact be wrong here:
`batman-adv-auto-gw-mode` disables dnsmasq's DHCP on `br-lan` and requests a
lease through `lan_dhcp`, which clashes with LibreMesh `anygw`, where **every
node serves DHCP** on the shared `10.100.0.1` gateway address. It is therefore
deliberately **not** installed. (`bat0` stays in `gw_mode client`.)

### What LibreMesh already does (verified in `libremesh/lime-packages`)

- `lime-hwd-openwrt-wan` / `lime-proto-wan` configure only the board's
  dedicated `wan` port (from OpenWrt's `board.json`). There is **no LibreMesh
  feature that turns a LAN port into a WAN port**, and the Aruba AP-303 has no
  `wan` interface at all.
- `watchping` (`system.watchping`, interface `wan`) pings through `wan` and
  runs the hooks in `/etc/watchping/wan-ok.d` and `wan-fail.d`.
- `babeld-auto-gw-mode` uses those hooks: while `wan` has real Internet it
  announces a default route (`proto 7`) to the mesh; when it fails it removes
  it. Any node with a working uplink thus becomes a gateway automatically,
  nodes route through the best one, and traffic fails over if it disappears.
  There is no manual master election.

### What `picot-autowan` adds (only the missing LAN-port piece)

A small daemon (`/usr/sbin/picot-autowan`, procd). The promoted port simply
**becomes the stock `wan` interface**, so all of the above works unchanged and
`picot-autowan` never touches routing or gateway modes itself:

1. Watches the physical Ethernet ports in `br-lan` for a link-up event. Ports
   already cabled when the daemon starts are probed only during the first 5
   minutes after boot, so it never interrupts your admin session.
2. Only acts if the node has no working uplink: no `wan` interface exists
   (Aruba) or the physical `wan` port has no cable (Redmi).
3. Detaches the port from the bridge for a few seconds and requests DHCP. The
   offer is rejected if malformed or inside the mesh subnet (another mesh node).
4. Pings public hosts (`1.1.1.1`, `9.9.9.9`) through the port. On success it
   sets `network.wan.device` to that port (`proto dhcp`), makes sure `wan` is in
   the firewall `wan` zone, and restarts `watchping`. The original settings are
   saved and restored on demotion. Otherwise the port goes back to the bridge.
5. If the promoted port's link stays down for 20 seconds it is returned to the
   LAN bridge, so a client or mesh cable can be plugged in there later.

```sh
python3 deploy-libremesh.py autowan --device aruba,ap-303 --host root@ARUBA_HOST
python3 deploy-libremesh.py autowan --device aruba,ap-303 --host root@ARUBA_HOST --action status
python3 deploy-libremesh.py autowan --device aruba,ap-303 --host root@ARUBA_HOST --action disable
```

`disable` stops the daemon and returns the promoted port to the bridge.
Settings live in `/etc/config/picot-autowan` (`probe_seconds`, `down_seconds`,
`ping_hosts`, and a space-separated `exclude_ports` to keep a port always LAN;
e.g. `uci set picot-autowan.settings.exclude_ports='lan1'`). The promoted
state is also stored there, so it survives a reboot.

Caveats (not yet tested on hardware; pilot on one node with console access):

- A port is detached from the bridge for about 10 seconds when a cable is
  plugged in. A laptop plugged into a LAN port sees a short blip and then
  stays on the LAN.
- LibreMesh's gateway hooks only follow the interface named `wan`, so a node
  has **one** uplink; redundancy comes from several nodes having uplinks.
- A promoted port is a plain WAN port: it no longer carries the wired mesh.
  On an Aruba AP-303 with a single Ethernet port, promoting it removes the
  wired path to the mesh (it keeps meshing over Wi-Fi).
- If the physical `wan` port of a Redmi is cabled, no LAN port is promoted.
  If a LAN port was promoted first and the `wan` cable is plugged in later, it
  is not used until the promoted port is unplugged.
- Re-running `lime-config` regenerates `br-lan`; the daemon removes the
  promoted port from the bridge again when it restarts.
- A device that offers Internet on a LAN port (including a phone hotspot or a
  rogue DHCP server) will be treated as an uplink. Use `exclude_ports` for
  ports that must never become WAN.
- The mesh subnet check assumes the node's LAN is `network.lan`.

## Firmware Selector and package selection

`deploy-libremesh.py` reads the live Firmware Selector configuration from
`https://firmware-selector.libremesh.org/`, then:

1. Resolves `latest` to the current stable OpenWrt release.
2. Checks the requested device profile in that release.
3. Reads that profile's device-specific and default packages.
4. Applies the Selector's LibreMesh default flavor and package compatibility
   substitutions (including the Selector's ath10k changes for Aruba).
5. Adds the editable packages in `firmware-selector-packages.txt` and any
   command-line packages.
6. Requests the custom image from the Selector's configured ASU build service.
7. Downloads the sysupgrade image and verifies the returned SHA-256.

There was no separate saved Selector parameter file in this directory. The
three default extras were recovered from a note in `script_config_lime_AP.txt`:

```text
wpad-mesh-openssl
luci
nano-full
```

They are listed one per line in `firmware-selector-packages.txt`. Edit that
file to change the normal package set. Lines beginning with `#` are comments.
You can add extra package arguments without editing the file:

```sh
python3 deploy-libremesh.py build \
  --device redmi-ax6000-ubootmod \
  --extra-packages "mwan3 luci-app-mwan3"
```

Repeat `--extra-packages` for additional groups. To omit the file's defaults:

```sh
python3 deploy-libremesh.py build \
  --device aruba-ap-303 \
  --no-default-extra-packages
```

Package availability can differ by architecture, release, and LibreMesh feed.
Review the build result and Selector/ASU error output; a package name being
accepted by this script does not prove that the package exists or fits the
device. In particular, do not add `mwan3` and assume it configures failover:
the router still needs a compatible package build and a separately designed
multi-WAN configuration.

## Build, stage, and deploy examples

Run commands from this directory on a computer with Python 3, OpenSSH `ssh`
and `scp`, and network access to the Selector and its ASU service.

### 1. Review parameters without requesting a build

```sh
python3 deploy-libremesh.py build \
  --device redmi-ax6000-ubootmod \
  --dry-run

python3 deploy-libremesh.py build \
  --device aruba-ap-303 \
  --dry-run
```

Dry-run retrieves current Selector metadata and prints the packages and
repositories that would be used. It does **not** request a build or change a
device.

### 2. Build an image only

```sh
python3 deploy-libremesh.py build \
  --device redmi-ax6000-ubootmod
```

By default, the script requests the current stable OpenWrt release and the
LibreMesh `master` feed. Pin a release or feed branch if needed:

```sh
python3 deploy-libremesh.py build \
  --device aruba-ap-303 \
  --version 25.12.6 \
  --feed-branch master
```

The image and a `.build.json` metadata file are written under
`firmware-output/`. The version in the example is illustrative; check Selector
availability before pinning an old release.

The build command can optionally send a first-boot UCI-defaults script to the
Selector:

```sh
python3 deploy-libremesh.py build \
  --device aruba-ap-303 \
  --uci-defaults ./reviewed-firstboot.sh
```

That script will execute on the device as part of first boot. Do not use
unreviewed scripts here. The deployment script itself does not currently
provide an automatic firstboot VLAN or mesh conversion.

### 3. Stage the image without flashing (recommended first device step)

Build and stage PICOT0, without flashing:

```sh
python3 deploy-libremesh.py deploy \
  --device xiaomi,redmi-router-ax6000-ubootmod \
  --host root@PICOT0 \
  --failsafe-dir backup-PICOT0-2026-10-30
```

For an Aruba AP-303 (OpenWrt board name; no failsafe-dir needed):

```sh
python3 deploy-libremesh.py deploy \
  --device aruba,ap-303 \
  --host root@ARUBA_HOST
```

Replace `ARUBA_HOST` with the actual SSH hostname, alias, or address reachable
from the deployment computer. SSH uses batch mode and strict host-key checking.
Verify/accept the correct host key in advance; do not disable host-key checking
to work around an error.

Staging checks the remote board identifier, copies the image and restore
script, makes a compressed copy of the live `/etc/config`, adds rollback
files to `/etc/sysupgrade.conf`, verifies the uploaded image hash, and runs
`sysupgrade -T` (image compatibility test). It does not reboot the AP unless
`--flash` is explicitly supplied.

With `--failsafe-dir DIR` (any directory holding a known-good `etc/config`,
e.g. `backup-PICOT0-2026-10-30`), staging additionally installs it, on any
device model, at:

```text
/root/libremesh-failsafe/picot0-config.tar.gz
```

On every device, staging saves that device's live configuration at:

```text
/root/libremesh-failsafe/live-config.tar.gz
```

Without `--failsafe-dir`, only the live capture is saved. Choose the failsafe
directory deliberately per node: it carries the hostname and addresses of the
node it was taken from.

### 4. Flash only after review

The following command builds, stages, asks for an exact confirmation, and
starts sysupgrade:

```sh
python3 deploy-libremesh.py deploy \
  --device redmi-ax6000-ubootmod \
  --host root@PICOT0 \
  --failsafe-dir backup-PICOT0-2026-10-30 \
  --flash
```

The prompt for the Redmi requires typing:

```text
FLASH xiaomi,redmi-router-ax6000-ubootmod
```

The Aruba confirmation is:

```text
FLASH aruba,ap-303
```

Do not flash an AP remotely unless you can recover it if it does not rejoin
the mesh. Make sure the image is for the exact board; keep a wired or serial
recovery path available. The upgrade uses `sysupgrade -c -v` to retain the
device configuration. Because this preserves the current settings, it is not
a method for deploying the proposed VLAN layout.

### Use an already downloaded image

If you already have a compatible Selector-built image:

```sh
python3 deploy-libremesh.py stage \
  --device aruba-ap-303 \
  --host root@PICOT-ap-cda908 \
  --image ./firmware-output/IMAGE.bin
```

Add `--flash` only after you have staged and checked the rollback. This
subcommand checks the local file hash during transfer and asks the remote
sysupgrade test to validate the image, but it does not independently prove
the image came from the Selector; use only a trusted image.

## Failsafe and restoring configuration

The saved PICOT0 backup is a **configuration backup**, not a firmware image.
During staging, the deploy script checks it has the required `network`,
`wireless`, `lime-node`, `firewall`, and `dhcp` files, creates a tar archive,
and puts it on PICOT0. It does not copy per-node generated configuration to
other devices.

The restore script is installed on each staged device at:

```text
/root/libremesh-failsafe/restore-libremesh-config.sh
```

### Restore the saved failsafe backup

Use a local console or SSH, then run (`picot0` is accepted as an alias of `saved`):

```sh
/root/libremesh-failsafe/restore-libremesh-config.sh saved
```

The script prints the board and hostname, verifies the archive paths and
required files, and asks for:

```text
RESTORE-SAVED
```

It will refuse to continue unless that exact token is entered.

### Restore the device's own pre-upgrade config

On a staged Aruba or Redmi:

```sh
/root/libremesh-failsafe/restore-libremesh-config.sh live
```

The script asks for:

```text
RESTORE-LIVE
```

Before replacing the UCI files, the script saves the current `/etc/config` to
a timestamped archive under `/root/libremesh-failsafe/`. It then restores the
selected archive, runs `lime-config` and `lime-apply`, and restarts the
network, Wi-Fi, firewall, and dnsmasq services.

### What rollback does not do

- It restores configuration only. It does **not** reinstall or downgrade
  firmware.
- It is useful only if the device boots and you can reach a shell. If the
  AP is unbootable or unreachable, use the device's supported recovery
  procedure or local/serial access.
- The `saved` restore applies the staged backup as-is to whatever node it was
  staged on, including that backup's hostname and addresses. Only stage a
  backup on a node it is appropriate for.
- A configuration restore can disconnect the session or change the device's
  management address. Have local access available.

The script archive and its backups contain sensitive configuration. The
remote rollback directory is listed in `/etc/sysupgrade.conf` so it should be
preserved through sysupgrade; verify this before flashing.

## Operational checks

Before building:

```sh
python3 deploy-libremesh.py build --device redmi-ax6000-ubootmod --dry-run
python3 deploy-libremesh.py build --device aruba-ap-303 --dry-run
```

On the device, verify board and configuration before staging:

```sh
ubus call system board
cat /etc/openwrt_release
uci show lime | grep -Eiv 'key|pass|secret|passwd'
ip -4 route show default
```

If `ubus` is unavailable, inspect:

```sh
cat /tmp/sysinfo/model /tmp/sysinfo/board_name
ip -o link show
```

Before adding WAN redundancy, inspect all default routes and determine why
there are already multiple routes. Do not infer the role of an Ethernet port
from DHCP, a gateway response, or Internet connectivity; the mesh/LAN may
share those paths.

## Troubleshooting

### SSH says the host key is unknown or changed

Stop and verify the host and its SSH fingerprint by a trusted method. The
deployment intentionally uses `StrictHostKeyChecking=yes`; it will not silently
accept a changed identity.

### Board mismatch

Do not bypass it. Check the device model, U-Boot layout, board identifier, and
selected `--device`. A Redmi AX6000 and Redmi AX6000 U-BootMod image are not
interchangeable.

### Selector reports a missing package or repository error

Run a dry-run, review the package list, remove the optional package, and retry
the build. The package list is read from the current Selector configuration,
but ASU build availability and feed contents may change.

### Image test fails

Do not flash. Confirm the image is a `sysupgrade` image for the selected
board/profile and that the downloaded file matches its build metadata.

### Device no longer reachable after upgrade/config restore

Try the local Ethernet management address and wait for mesh convergence. If
the device does not boot or cannot be reached, use local/serial recovery. The
configuration restore command is not a substitute for firmware recovery.

## Emulator testbed (experimental)

`emulator/lime_testbed.py` is an unprivileged QEMU/KVM harness (no root, no
TAP devices) intended to test the scripts on several virtual nodes:

- each node is an x86-64 OpenWrt/LibreMesh VM; links between nodes are
  Ethernet segments switched by a small userspace hub, and a link can be cut
  and restored at runtime to simulate failover;
- every VM can get a management NIC with an SSH port forwarded on
  `127.0.0.1`; `Testbed.setup_ssh()` writes an ssh config that
  `deploy-libremesh.py` uses when `LIBREMESH_SSH_CONFIG=<file>` is set
  (strict host-key checking and BatchMode stay on);
- the serial console is driven directly, so no extra Python packages are
  needed. `emulator/images/` and `emulator/state/` are git-ignored.

Status (2026-11-01): a Firmware Selector x86/64 LibreMesh image was built and
verified. Three-node tests passed for bidirectional pings, batman-adv
neighbours, Babel daemon startup, and continued A-to-C connectivity after
cutting the direct A-C link. A separate stock OpenWrt test also verified that
cutting a link stops traffic on that segment. The Babel test checks that the
daemon is running; it does not verify Babel-learned routes. VLAN apply,
rollback, autowan, and real-hardware tests are still outstanding. See the
[test journal](./emulator/TEST-JOURNAL.md) for exact results.

The harness detaches management/mesh ports before assigning test addresses.
Stock x86 images may bridge Ethernet ports into `br-lan`; do not assume port
roles from their names.

Limits: there is no QEMU machine for the Aruba's IPQ40xx SoC, so the emulator
cannot reproduce ath10k/mt7915 radios, real Wi-Fi mesh, DSA/switch port
naming or Aruba hardware quirks. It checks logic and scripts only.

## Design and change log

- Existing client SSID and mesh ID were read from PICOT0's live configuration
  and dated backup. Wi-Fi keys are deliberately not reproduced here.
- The older July Xiaomi backup has different generated addressing and mesh
  settings. It is an archive for reference, not the current PICOT0 restore
  point.
- VLAN 10/20 support is implemented as an optional per-node operation, but has
  not been applied or tested on live hardware. VLAN 30, Ethernet port
  assignment/tagging remain future work. Automatic WAN detection
  (`picot-autowan`) is likewise implemented but untested on hardware.
- The VLAN transport and generated device/radio names must be verified on a
  pilot device before fleet rollout. Keep a local recovery path available.
