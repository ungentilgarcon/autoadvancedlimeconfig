# Emulator test journal

## 2026-11-02

### VXLAN implementation and verification status

- Replaced the managed `bat0.10`/`bat0.20` transport with separate VXLAN
  interfaces and bridges (VNIs 1010/1020), gateway broadcast-replication
  peers, and edge-to-gateway peers. Bridge-loop avoidance remains enabled.
- Added `vxlan` and `ip-bridge` to the default firmware package list and
  gateway/edge underlay address fields to the config templates.
- Built an Aruba AP-303 sysupgrade image after removing the Selector profile's
  conflicting `wpad-basic-mbedtls` package. The verified image is
  `openwrt-25.12.6-7946027b7240-ipq40xx-generic-aruba_ap-303-squashfs-sysupgrade.bin`
  (SHA-256 `a0a280c4e46e145252dd40dff783827ff110c2f62a079642b8db62e73a412bf5`).
  It has not been staged or flashed.
- Added `build --inject-pass` to create a credentialed image with the matching
  local OpenWrt ImageBuilder. It reads private `.pass` values locally and does
  not submit them to ASU. The AP-303's `lime-node.wifi` first-boot settings
  reuse the saved PICOT0 main-Wi-Fi and mesh credentials; the Aruba keys in
  `.pass` are populated locally and remain ignored by Git.
- A local AP-303 ImageBuilder build succeeded. Output:
  `firmware-output/openwrt-25.12.6-ipq40xx-generic-aruba_ap-303-squashfs-sysupgrade-with-pass.bin`
  (SHA-256 `b22ea1f7976a2965cf8c4a7f97479aa3c62d34a8edb4deb3f05d9497cc42066d`).
  The resulting image contains readable network credentials; its mode is
  owner-only. It has not been staged or flashed.
- First-boot defaults only apply on a fresh install/config reset. The existing
  deploy flash path preserves configuration, so the new `--inject-pass`
  option is blocked with `deploy --flash` rather than silently producing an
  image whose Wi-Fi defaults do not run. AP-303 radio and Ethernet behavior
  still require local recovery access and a hardware pilot.
- The complete `python3 emulator/run_scenarios.py --image ...` suite passed:
  three-node mesh pings and failover; VXLAN underlay with BLA enabled; apply,
  confirmation, DHCP, overlay ping, disable, and explicit rollback; and
  autowan promotion, connectivity, and demotion.
- The runner isolates autowan on its own ISP topology. It also resolves the
  management NIC by MAC and delays serial-console probing until the VM boots,
  avoiding the earlier combined-topology and GRUB-input failures.
- Explicit rollback was tested; the automatic 180-second rollback timer was
  not allowed to expire. This x86 image cannot test real-radio association,
  Aruba/Xiaomi hardware, or actual router flashing. No physical device was
  changed.

## 2026-11-02

### Earlier VXLAN feasibility (superseded by the verification above)

- Added the OpenWrt `vxlan` protocol package to the default firmware package
  list; it depends on the kernel-matched `kmod-vxlan`.
- An x86/64 image containing `kmod-vxlan` loaded the kernel module. Installing
  the separate `vxlan` protocol package in an ephemeral VM allowed netifd to
  create an up VXLAN interface with MTU 1450 after a network restart.
- The full build with the `vxlan` protocol package exceeded the 150-second ASU
  build limit. The earlier image had only the kernel module.
- End-to-end DHCP and mesh transport over VXLAN are **not verified**. Mesh
  reachability did not come up reliably in the attempted test run; do not use
  the current VLAN script on live nodes.

## 2026-11-01

### Image

- Built an OpenWrt 25.12.6 x86/64 LibreMesh image through Firmware Selector
  ASU, with `kmod-mac80211-hwsim`.
- ASU returned a `squashfs-combined` image rather than a `sysupgrade` image.
  Added `--image-type combined` to the deploy tool; the downloaded image
  checksum was verified by the tool.
- The image is for QEMU x86/64, not Aruba AP-303 hardware.

### Passed

- Three stock OpenWrt VMs: A-B and B-C links passed bidirectional pings
  (2/2); cutting B-C stopped the ping (0/2).
- Three LibreMesh VMs on a triangle: unique `bat0` test addresses
  `10.77.0.1`–`10.77.0.3` passed A-B, B-C, and C-A pings.
- `batman-adv` neighbours were present on A and the `babeld` process was
  running. Babel-learned route exchange was not verified.
- After cutting direct A-C, A still reached C through B. Restoring the link
  succeeded.
- QEMU GRUB required an explicit monitor `sendkey ret`. The virtual
  management NIC also had to be removed from LibreMesh's mesh/bridge before
  its forwarded SSH connection worked. One-node SSH setup then passed.

### Not yet tested (as of 2026-11-01)

- VLAN apply/confirm/disable and timed rollback. A VLAN run was started but
  did not return a result.
- Automatic WAN promotion/demotion.
- Firmware staging with `sysupgrade -T`, flash, or any real Aruba/Xiaomi
  hardware behavior.
- Wi-Fi mesh and Aruba IPQ40xx/DSA/switch behavior cannot be represented by
  these x86 VMs.
