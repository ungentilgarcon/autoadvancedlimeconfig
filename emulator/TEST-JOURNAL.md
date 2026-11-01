# Emulator test journal

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

### Not yet tested

- VLAN apply/confirm/disable and timed rollback. A VLAN run was started but
  did not return a result.
- Automatic WAN promotion/demotion.
- Firmware staging with `sysupgrade -T`, flash, or any real Aruba/Xiaomi
  hardware behavior.
- Wi-Fi mesh and Aruba IPQ40xx/DSA/switch behavior cannot be represented by
  these x86 VMs.
