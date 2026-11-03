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
| `fleet-libremesh.py` | Experimental controller for firmware and coordinated configuration changes on multiple nodes. |
| `fleet-config-ui.py`, `fleet_archive.py` | Local backup comparison, per-node dry run, and experimental staged fleet deployment. |
| `picot-config-sync`, `picot-config-recovery` | On-node configuration staging, timed rollback, and boot recovery helpers installed by the fleet controller. |
| `fleet-inventory.example.json`, `fleet-changes/*.example.json` | Illustrative fleet inventory and change templates; replace values before use. |
| `restore-libremesh-config.sh` | On-device interactive restore for the saved PICOT0 config or the device's captured pre-upgrade config. |
| `apply-libremesh-vlans.sh` | Applies or removes managed VLAN UCI sections on a device with a timed rollback and explicit confirmation. |
| `picot-vlan-recovery` | Boot-time recovery hook; restores a pending transaction before network startup if the node reboots. |
| `.pass.example` | Credential-file template; private `.pass` is ignored by Git and supplies shared Wi-Fi/mesh keys, AP hostname, optional root password, and VLAN SSID keys. |
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

Not sure of the exact name? Search the current release's profile list (all
words must match the id, vendor or model); use the first column as `--device`:

```sh
python3 deploy-libremesh.py build --list-devices redmi ax6000
```

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

The VLAN script transports VLAN 10 and 20 as separate VXLAN overlays (VNIs
1010 and 1020) over the existing `lan`/`br-lan` mesh-reachable IPv4 network.
The firmware needs the OpenWrt `vxlan` and `ip-bridge` packages; the latter
provides gateway broadcast replication to each edge. Bridge-loop avoidance
stays enabled. Configure the gateway with one `peer_ips` entry for every
VLAN-enabled edge, and each edge with the gateway's mesh-reachable LAN IP as
`gateway_ip`. The network underlay and UDP 4789 must be reachable between
those addresses. End-to-end QEMU results and the remaining hardware limits are
recorded in `emulator/TEST-JOURNAL.md`. Do not apply to a live node unless the
current QEMU scenario passes and you have local recovery access. VLAN 30 is not
created or assumed.

Before configuring the overlay, check each node's underlay address with
`ip -4 addr show dev br-lan` and verify gateway-to-edge reachability with
`ip route get EDGE_IP` and `ping EDGE_IP`. Use stable, unique addresses on the
existing mesh LAN; do not assign these addresses to the new VLAN bridges.

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
Git. `MAIN_WIFI_KEY` and `MESH_WIFI_KEY` are shared by Aruba and Redmi builds;
the current PICOT0 values were copied from the saved config, and the backups
remain unchanged. Set `AP_HOSTNAME` to the LibreMesh hostname template for
these nodes. The template's current value, `PICOT-ap-%M4%M5%M6`, gives each
node a MAC-derived name. The file also has VLAN SSID keys; fill those locally.
`OPENWRT_ADMIN_PASSWORD` is an optional shared root password for locally built
images; leave it blank to keep the root password unchanged.
The VLAN deployer requires
`IOT_WIFI_KEY` and `GUEST_WIFI_KEY`, each 8–63 printable ASCII characters.
Use the same value per SSID across APs if clients should roam. The router
deployment commands still use SSH keys and strict host-key checking.

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
For each other VLAN-enabled AP, make an edge config from
`vlan-configs/edge.example`, set the proper radios and `gateway_ip`, then use
the matching device profile and SSH host. Add every edge's mesh-reachable LAN
IPv4 address to the gateway config as a `list peer_ips` value. Use addresses
that are reachable over the existing `lan` interface; do not use a client
VLAN address or an address that changes with DHCP.
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
AP with local recovery available. Verify `picot_vx_iot` /
`picot_vx_guest`, `br-picot-iot` / `br-picot-guest`, each new SSID, DHCP,
guest Internet access, IoT WAN isolation, and access from the existing main
network before confirming. Confirm that the gateway can reach every configured
edge underlay IP and that UDP 4789 is allowed between them. The change is applied by direct UCI updates;
it does not invoke `lime-config`, which could regenerate LibreMesh-managed
network or wireless sections.

### Return a node to normal behaviour (no VLANs)

If VLANs do not work on a node, remove them from just that node:

```sh
python3 deploy-libremesh.py vlans --device aruba,ap-303 --host root@ARUBA_HOST --disable
```

This uses the same timed-rollback flow and prints a `VLAN_CHANGE_ID` to
confirm. It removes only the managed VXLAN interfaces/bridges, VLAN SSIDs, and
DHCP/firewall entries, leaving the main untagged network, mesh, and Ethernet
settings unchanged. A node without these SSIDs continues to use the main mesh,
but does not host the VLAN overlays. If an enabling change fails or is not
confirmed, the automatic rollback returns the node to its previous state,
which is non-VLAN if it had none.

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
6. Requests the image from the Selector's ASU build service, or builds locally
   when private credentials, model fixes or custom defaults are included.
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
The `-wpad-basic-mbedtls` entry removes the Selector profile's default package,
which conflicts with the required `wpad-mesh-openssl`.
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

#### Per-model fixes

Place files in `fixes/<target>/<subtarget>/<profile>/files/` to embed them
automatically in matching firmware builds. For example, the Aruba AP-303
directory is `fixes/ipq40xx/generic/aruba_ap-303/files/`. Redmi AX6000 ubootmod
uses `fixes/mediatek/filogic/xiaomi_redmi-router-ax6000-ubootmod/files/`.
The dry run lists matching paths; other models are unaffected. See
[fixes/README.md](fixes/README.md) for safety and upgrade limitations.

The `fixes/` directory is **not Git-ignored**; keep credentials and sensitive
configuration outside the repository. To use a private directory with the
same structure instead, pass `--fixes-dir /path/to/private-fixes` to `build`,
`deploy`, or `fleet-libremesh.py firmware`. The selected directory replaces
the default rather than merging with it, so it must contain all desired fixes.

Overlays are built locally with ImageBuilder, not sent to ASU. GNU `gawk` and
a matching ImageBuilder are required, as with
`--inject-pass`. The build metadata records the embedded file paths and
SHA-256 hashes. Embedding a file does not itself activate a fix; saved
configuration may override image files during a configuration-preserving
upgrade. The AP-303 overlay includes the experimental radio watchdog described
below; test it on one node before wider deployment.

#### AP-303 radio recovery

AP-303 builds using the repository's default model fixes include an enabled
`picot-radio-recovery` service and `/etc/config/picot-radio-recovery`. It
monitors only the observed `radio0`/`wlan0-mesh_17` path. When an established
mesh peer exists and Babel has selected that interface as the default route,
it maps the IPv4 next hop to its established mesh peer and probes that peer's
IPv6 link-local address and two public IP addresses every 30 seconds. After
three failed checks against the same next hop, with no more than ten minutes
between failures, it restarts **only radio0**. Checks on another selected
route do not erase the failures, but no restart happens while that route is
selected. A successful check, loss of all established peers, or a ten-minute
gap resets the failure count. There is a 30-minute cooldown and a limit
of four restart attempts per boot. A stop or failed restart triggers a
best-effort attempt to bring the radio back up.

The checks cannot distinguish every failed peer or upstream outage from a
radio fault, and restarting interrupts 2.4 GHz clients and mesh links. This is
an experimental recovery, not a proven fix for the underlying driver or peer
problem. It does nothing without an established peer or a selected Babel
default via `wlan0-mesh_17`, and does not monitor the 5 GHz radio. If Babel
has switched to another route, the faulty link may remain undetected. The
watchdog also declines to restart if it cannot map the next hop to an
established IPv6 peer, for example if there is no usable neighbor entry and
no mapping cached since boot. The public ICMP probes can be filtered, and
rebooting resets the attempt limit.
Review logs with `logread -e picot-radio-recovery`.

To disable it persistently, run `uci set picot-radio-recovery.settings.enabled=0`,
`uci commit picot-radio-recovery`, and `/etc/init.d/picot-radio-recovery stop`.
Set `enabled` to `1`, commit, and start the service to re-enable it. The image
contains a boot service link; the generated configure command also enables and
starts the service if present, but respects the UCI `enabled` setting.
An external `--fixes-dir` replaces these files, and prebuilt images are
unchanged. A real AP-303 image was built and its watchdog files and boot link
inspected; the watchdog and a configuration-preserving upgrade have not been
tested on hardware.

#### Embed Wi-Fi and mesh credentials locally

To include the saved Aruba Wi-Fi and mesh credentials in a first-boot setup,
use the explicit local-only option:

```sh
python3 deploy-libremesh.py build \
  --device aruba-ap-303 \
  --inject-pass
```

This reads the shared `MAIN_WIFI_KEY`, `MESH_WIFI_KEY`, and `AP_HOSTNAME` from
the private `.pass` file, then uses the matching OpenWrt ImageBuilder on this
computer. The same credential fields are used for all profiles supported by local
credential injection.
The ImageBuilder is downloaded from the OpenWrt release site and cached under
`~/.cache/libremesh-deploy/`; the values are not sent to Selector/ASU.
Install GNU `gawk` if the local build reports it missing. An already extracted
matching ImageBuilder can be selected with `--imagebuilder-dir PATH`.

Optionally set `OPENWRT_ADMIN_PASSWORD` in `.pass` to set the device's root
password. This shared field applies to every device supported by the local
credential-injection path. Use 8–128 printable ASCII characters; leave it
blank to leave the root password unchanged. The build hashes it locally with
SHA-512 crypt and embeds only the hash in the first-boot and recovery scripts.
The plain password is not sent to Selector/ASU. This does not enable SSH
password authentication; deployment continues to use SSH keys.

The resulting sysupgrade image contains the credentials in readable form and
is written owner-only under `firmware-output/`; do not commit, publish, or
send it to anyone. The first-boot script sets the PICOT AP hostname template
and Wi-Fi/mesh settings before LibreMesh's `91_lime-config` defaults script
generates the device config.
Preserving an existing configuration with `sysupgrade -c` prevents first-boot
defaults from running; do not use `deploy --inject-pass --flash`. Install with
a supported clean-install/config-reset method only when local recovery access
is available. This does not guarantee Aruba radio or Ethernet behavior, which
still needs a device pilot.

The image also includes a root-only recovery command at
`/usr/sbin/autoadvancedlimeconfig-configure`. If the device boots but the
first-boot settings did not take effect, run it from a root SSH or
serial-console session. It asks for `APPLY`, backs up the current `lime-node`,
`network`, and `wireless` UCI files under `/root/`, sets the same hostname and
Wi-Fi/mesh settings, then regenerates and applies LibreMesh configuration. If
an admin password is configured, it also backs up `/etc/shadow` and updates
the root password. The network restart may disconnect your session; use local
recovery access.
This recovery command is only present in the rebuilt credentialed image.

The build command can optionally embed a first-boot UCI-defaults script using
the local ImageBuilder:

```sh
python3 deploy-libremesh.py build \
  --device aruba-ap-303 \
  --uci-defaults ./reviewed-firstboot.sh
```

That script is intended to execute on a clean first boot; it is not guaranteed
to run when existing configuration is preserved. Do not use unreviewed
scripts here. Its contents remain local and are not sent to Selector/ASU.
The deployment script does not provide an automatic firstboot VLAN conversion.

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

## Fleet upgrades (experimental)

### Backup comparison and fleet deployment

Run `python3 fleet-config-ui.py` on the controller and open the printed local
URL. Upload an original router backup and an edited copy, and/or enter
comma-separated package names. The page lists changed regular files under
`/etc`, `/usr/sbin` and `/opt`, their permissions, and line counts without
displaying file contents. To review a new tool, add it under `usr/sbin/` or
`opt/` in the edited archive and set its executable permissions before
repacking. Other paths, including `/usr/bin`, are not included. OpenWrt backups
do not necessarily contain `/usr/sbin` or `/opt` by default. Backups remain in
the local process/browser memory and are not written into the repository.
The URL contains an access token; do not share it. The interface listens only
on `127.0.0.1`.

The UI queries nodes over SSH for their live OpenWrt model and board ID.
Automatic mesh discovery is enabled by default. It reads LibreMesh shared DNS
records from a reachable inventory node, or from the optional mesh seed SSH
host. The inventory field is filled automatically only if
`fleet-inventory.json` exists in the controller's current directory. To use
only a seed without an inventory, leave the inventory field empty and enter
its SSH host (for example `root@10.100.159.52`). The seed's own hostname
must be present in shared DNS. The seed can use SSH keys or the shared
`FLEET_SSH_PASSWORD`; an explicit seed uses the node-specific password key
`FLEET_SSH_PASSWORD_MESH_SEED` if configured.

Shared DNS can include clients as well as routers. Each candidate must accept
SSH, report a matching hostname and OpenWrt board, and have `lime-config`
installed before it is added to the targets. Unverified candidates are listed
with their errors and excluded, including routers whose SSH host keys have not
been verified or credentials are unavailable. Verify host keys by a trusted
method and add them to the controller's known hosts; discovery never accepts
unknown keys. Disable discovery to use only the inventory.

Discovery depends on the seed's current
`/tmp/hosts/shared-state-dnsmasq_hosts`, which can be stale or incomplete;
it cannot guarantee detection of every mesh member. It does not scan the
entire subnet or prove deployment health. Use **Refresh available nodes** to query
again. The dry run performs discovery again and reports its exact target list
and the names and errors of excluded candidates; review both before confirming
a deployment.

Choose **All inventory and discovered routers** (the default), **Node names**, or **Models**.
For node names and models, enter exact comma-separated values shown in the
table, for example `ap-1, ap-2` or
`aruba_ap-303, xiaomi_redmi-router-ax6000-ubootmod`. Model selection uses the
displayed model ID derived from the live board ID, not the inventory's
`device` field. Model targeting fails if any inventory node cannot be
identified, so an unavailable matching node is not silently omitted. To
target a partial fleet, explicitly select node names instead. Unknown,
duplicate, or empty entries are rejected. Only the selected nodes are
preflighted and deployed; their peer health checks likewise cover only the
selected nodes. Model-targeted nodes are checked again before deployment, and
a changed or unavailable board invalidates the plan. Discovered targets also
have their hostname checked again before deployment.

Select files and/or packages and an inventory, then choose **Dry run against
nodes**. For package-only deployments, entering package names enables the
dry run immediately; no backups or comparison step are needed. The dry run fetches
current files over SSH and checks whether each change can be merged without
overwriting per-node differences. Every node needs a valid, distinct `mesh_ip`.
Changed symlinks and special files are rejected.

The dry run detects `apk` (OpenWrt 25.12+) or `opkg` on each node. APK
simulates dependency resolution against cached indexes; OPKG checks requested
package metadata only, **not dependencies**, because its no-action install
cannot reliably simulate a new download. Update indexes beforehand using
`apk update` or `opkg update` on each node if necessary. The dry run does not
install packages, validate UCI syntax, check connectivity to repositories, or
check node health.

After a successful dry run, choose **Deploy selected changes** and type
`DEPLOY` to confirm. The controller checks package availability and node
health, installs requested packages sequentially, stages files on every node,
then schedules the rollout. Target files are checked again before apply;
stale files abort the change. The nodes must have `flock`, and the current
file and UCI helpers serialize operations using a shared lock. Do not run
older controller/helper versions concurrently. Changes under `/etc/config`
are checked for UCI syntax during staging, not during the dry run.

Each node's package manager automatically resolves and retrieves required
dependencies from its configured repositories. Installation fails if it
cannot obtain them; OPKG's dry run does not detect every such failure.

**Package installation is not rolled back**, including dependencies, upgrades
and package scripts. A failure or timeout may leave a partially installed or
still-running operation; the error identifies attempted and completed nodes.
Package scripts can change configuration or connectivity before file staging.
If they change a selected file, staging rejects the stale preimage; run a new
dry run after examining the node.

Files retain their selected modes; modifications also preserve the existing
node's ownership. A watchdog and boot recovery restore pending, unconfirmed
changes. Scheduling and confirmation are **best effort, not atomic**: a
partial confirmation or an unreachable node can leave different states and
require manual recovery. Health checks require peer pings and, where its init
script exists, Babel; they do not prove routing convergence or validate a
different routing protocol. `Reload configuration` calls OpenWrt
`reload_config`; `Write files only` requires separate activation as needed.
The helper retains staged files and rollback snapshots on each node under
root-only `/root/libremesh-failsafe`; treat them as sensitive.

Three isolated QEMU nodes passed deployment, stale-target, locking, watchdog
and reboot-recovery scenarios. A mixed APK/OPKG QEMU fleet passed package-only,
combined and partial-failure tests. This does not prove safety on physical
devices. The separate UCI-only controller
rolls back `/etc/config`, not arbitrary files under `/etc`, `/usr/sbin` or
`/opt`.

**Do not use this controller on a production fleet without a successful
hardware pilot and independent recovery access to every node.** QEMU cannot
prove real Wi-Fi connectivity, firmware recovery, or atomic fleet-wide
changes. Keep a verified configuration backup and a local/serial recovery
path. In particular, automatic firmware rollback is not provided.

The controller supports:

- `firmware`: build or supply one image per device profile, then upgrade
  nodes sequentially with `sysupgrade -c`, checking health and stopping on
  failure. Canary nodes go first, ordinary nodes next, and gateways last
  (do not mark a gateway as a canary if it must remain last).
- `config-safe`: stage UCI changes on all affected nodes, then apply and
  confirm each node sequentially with a timed rollback. An allowlist guard
  is only a heuristic, not proof that a change cannot disrupt connectivity.
- `config-breaking`: stage on all affected nodes, measure clock offsets, arm
  a scheduled apply, and check health before confirming. This is
  **best-effort synchronization, not an atomic transaction**. Failures
  during arming or confirmation can result in different node states; check
  every node and be ready to recover it locally.

### Prepare an inventory and access

Copy `fleet-inventory.example.json` to `fleet-inventory.json` and edit the
copy. Each node needs a unique `name` and SSH `host`; specify the exact
OpenWrt `device` profile for firmware upgrades, `role` (`node` or `gateway`),
optional `canary`, and a reachable `mesh_ip` for peer checks. Obtain device
profile IDs with `python3 deploy-libremesh.py build --list-devices`.
Missing `mesh_ip` values are omitted from checks, so include one for **every**
node if peer reachability matters. The controller checks only the selected
inventory nodes and only applies a change to nodes with UCI lines.

Configure SSH keys and verify each host key beforehand; strict host-key
checking is enabled. If SSH passwords are needed, copy `.pass.example` to
`.pass`, make it mode 600, and set `FLEET_SSH_PASSWORD` or the per-node
`FLEET_SSH_PASSWORD_<NODE>` override. The suffix is the node name uppercased
with non-alphanumeric characters replaced by underscores. Password-based
SSH requires `sshpass`; otherwise SSH keys are used. Firmware board checks
currently use the deploy tool's key-based SSH path even when a fleet password
is configured, so password-only firmware access is not supported.

`.pass` is not interpolated into UCI change files: if a configuration change
needs a secret, arrange it privately and protect the change file itself.
Dry-run prints UCI lines, and validation errors can print them too; avoid
exposing terminal output. Git ignores the default `fleet-inventory.json`,
non-example files directly under `fleet-changes/`, `fleet-backups/`,
`fleet-logs/`, and `firmware-output/`. **Other paths and names are not
automatically private.** Use `git check-ignore -v PATH`, verify `git status`
before committing, and protect backups and custom paths separately. Set
`umask 077` before creating private files or running backups; Git ignore is
not access control.

### Configuration changes

Copy an example to an ignored name, for example
`cp fleet-changes/safe.example.json fleet-changes/my-change.json`, then edit
it for the actual nodes. Change JSON contains `kind`, an `apply` mode
(`lime`, `reload`, or `none`), a common `uci` list of UCI batch commands,
and optionally `node_uci`, mapping node names to additional command lists.
`lime` runs `lime-config` and `lime-apply`; `reload` invokes
`reload_config`; `none` only commits UCI changes. Select the appropriate
mode and verify the resulting services; examples are not production
configuration.

```sh
python3 fleet-libremesh.py backup
python3 fleet-libremesh.py config fleet-changes/my-change.json --dry-run
python3 fleet-libremesh.py config fleet-changes/my-change.json --id change-001
python3 fleet-libremesh.py status
```

By default the controller reads `fleet-inventory.json` in the current
directory; use `--inventory PATH` to select another. Repeat `--node NAME` to
limit a command, but doing so excludes other nodes from the staging and
health gates. `--window` sets the rollback timeout (default 600 seconds);
`--lead` sets the breaking-change scheduling delay (default 45 seconds).
`--force` bypasses preflight health failures and `--no-safety-guard` bypasses
the safe-change guard; neither makes a change safe.

On each affected node the controller installs `picot-config-sync` and enables
`picot-config-recovery`. A staged change is tested against a scratch copy of
UCI configuration. Before applying, the node saves `/etc/config` under
`/root/libremesh-failsafe/`; a watchdog restores it if the change is not
confirmed before its deadline, and the recovery hook attempts restoration
on reboot. Snapshots contain secrets. A pending change can be rolled back
with `python3 fleet-libremesh.py rollback change-001` or, locally on a node,
`/usr/sbin/picot-config-sync rollback change-001`. Rollback only works while
the change is pending, not after confirmation. A partial confirmation may
leave confirmed nodes on the new config while other nodes revert; inspect
all nodes and use local recovery as needed.

### Firmware

```sh
python3 fleet-libremesh.py firmware --dry-run
python3 fleet-libremesh.py firmware --version VERSION
# Or provide an image for every selected profile:
python3 fleet-libremesh.py firmware --image 'DEVICE=/private/path/image.bin'
```

The controller asks for an explicit fleet confirmation, tests each uploaded
image using `sysupgrade -T`, and stops when a node fails its checks. It
preserves configuration via `sysupgrade -c`; this does not guarantee that an
old configuration works with a new firmware release. Firmware may not be
recoverable automatically. Test the exact board, image, SSH access and
restore procedure on a pilot first. The backup command saves only
`/etc/config`; it is not a full firmware backup.

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

The earlier QEMU suite reported passes for three-node mesh and failover,
overlay reachability, VLAN apply/confirm/disable, explicit rollback, and
automatic WAN promotion/demotion. **The reported ping-based mesh and
reachability results are not reliable:** several checks suppress ping output
and then check for empty output without checking the exit status. They must be
rerun with corrected assertions before claiming connectivity or failover.
The runner uses a separate ISP topology for autowan to avoid a
management-network collision. Run it with
`python3 emulator/run_scenarios.py --image /path/to/openwrt-x86-64.img`.
That older suite did not test automatic timeout rollback, real Wi-Fi, or
router hardware behavior. See the [test journal](./emulator/TEST-JOURNAL.md)
for exact results.

The fleet-specific runners use isolated VM disks and SSH state:

```sh
python3 emulator/run_fleet_scenarios.py --image /path/to/libremesh-x86.img
python3 emulator/run_fleet_file_scenarios.py --image /path/to/libremesh-x86.img
python3 emulator/run_fleet_one_package_scenario.py --image /path/to/libremesh-25.12-x86.img
python3 emulator/run_fleet_package_scenarios.py \
  --apk-image /path/to/libremesh-25.12-x86.img \
  --opkg-image /path/to/openwrt-24.10-x86.img
```

The package test downloads package indexes and installs packages in the
disposable VMs; it requires internet access. These tests do not establish
physical radio behavior or atomic fleet-wide deployment.

The harness detaches management/mesh ports before assigning test addresses.
Stock x86 images may bridge Ethernet ports into `br-lan`; do not assume port
roles from their names.

For the fleet controller, run
`python3 emulator/run_fleet_scenarios.py --image /path/to/openwrt-x86-64.img`
with a raw LibreMesh image. This separate three-node test uses isolated SSH
state and verifies peer pings, a safe change, a scheduled change, rejection
of an invalid UCI command, and watchdog rollback. The scheduled change uses
`apply: none` and does not actually disrupt connectivity. It does not test
firmware flashing, boot-time recovery, a failed arming or confirmation, or
real hardware. See the [test journal](./emulator/TEST-JOURNAL.md) for results.

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
