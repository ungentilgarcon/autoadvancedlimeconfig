import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "fixes/ipq40xx/generic/aruba_ap-303/files"
SCRIPT = OVERLAY / "usr/sbin/picot-radio-recovery"

MOCK = r"""#!/bin/sh
case "${0##*/}" in
    uci) echo "${MOCK_ENABLED:-1}" ;;
    ubus)
        case "$*" in
            *"network.wireless status"*) echo status ;;
            *"network.wireless down"*)
                echo down >> "$MOCK_CALLS"
                [ "${MOCK_DOWN_FAIL:-0}" = 0 ] ;;
            *"network.wireless up"*)
                echo up >> "$MOCK_CALLS"
                [ "${MOCK_UP_FAIL:-0}" = 0 ] ;;
            *) exit 1 ;;
        esac ;;
    jsonfilter)
        case "$*" in
            *".radio0.up"*) echo "${MOCK_RADIO_UP:-true}" ;;
            *".ifname"*) echo "${MOCK_IFNAME:-wlan0-mesh}" ;;
            *) exit 1 ;;
        esac ;;
    iw)
        case "$*" in
            *"station dump"*) [ "${MOCK_PEERS:-1}" = 1 ] && echo 'mesh plink: ESTAB'; exit 0 ;;
            *"station get"*) [ "${MOCK_PEERS:-1}" = 1 ] && echo 'mesh plink: ESTAB'; exit 0 ;;
            *" info") exit 0 ;;
            *) exit 1 ;;
        esac ;;
    ip)
        case "$*" in
            "link show dev wlan0-mesh_17") exit "${MOCK_LINK_FAIL:-0}" ;;
            "-4 route get 1.1.1.1")
                echo "1.1.1.1 via ${MOCK_HOP:-10.100.159.52} dev ${MOCK_DEV:-wlan0-mesh_17} src 10.100.169.8" ;;
            "-4 route show default proto babel")
                [ "${MOCK_BABEL:-1}" = 1 ] && echo "default via ${MOCK_HOP:-10.100.159.52} dev ${MOCK_DEV:-wlan0-mesh_17} onlink"
                exit 0 ;;
            "-4 neigh show to "*" dev wlan0-mesh_17")
                [ "${MOCK_MAPPING:-1}" = 1 ] && echo "${MOCK_HOP:-10.100.159.52} dev wlan0-mesh_17 lladdr ca:d8:43:0e:9f:35 REACHABLE"
                exit 0 ;;
            "-6 neigh show dev wlan0-mesh_17")
                [ "${MOCK_MAPPING:-1}" = 1 ] && echo "fe80::c8d8:43ff:fe0e:9f35 lladdr ca:d8:43:0e:9f:35 router REACHABLE"
                exit 0 ;;
            *) exit 1 ;;
        esac ;;
    ping)
        for arg in "$@"; do target=$arg; done
        case "$target" in
            1.1.1.1|9.9.9.9) [ "${MOCK_INTERNET:-0}" = 1 ] ;;
            *) [ "${MOCK_HOP_OK:-0}" = 1 ] ;;
        esac ;;
    logger) echo "$*" >> "$MOCK_LOG" ;;
    sleep) exit 0 ;;
    *) exit 1 ;;
esac
"""


class RadioRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        dispatcher = self.bin / "dispatcher"
        dispatcher.write_text(MOCK, encoding="utf-8")
        dispatcher.chmod(0o755)
        for name in ("uci", "ubus", "jsonfilter", "iw", "ip", "ping", "logger", "sleep"):
            (self.bin / name).symlink_to(dispatcher)
        self.board = self.root / "board"
        self.board.write_text("aruba,ap-303\n", encoding="utf-8")
        self.state = self.root / "state"
        self.calls = self.root / "calls"
        self.log = self.root / "log"
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "PICOT_RADIO_RECOVERY_STATE_DIR": str(self.state),
            "PICOT_RADIO_RECOVERY_BOARD_FILE": str(self.board),
            "MOCK_CALLS": str(self.calls),
            "MOCK_LOG": str(self.log),
        }

    def check(self, **values):
        env = {**self.env, **{f"MOCK_{key}": str(value) for key, value in values.items()}}
        return subprocess.run(
            [str(SCRIPT), "check"], env=env, text=True, capture_output=True, check=False
        )

    def calls_made(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def failures(self):
        return int((self.state / "failures").read_text())

    def test_requires_three_failures_and_only_restarts_radio0(self):
        self.assertEqual(self.check().returncode, 0)
        self.assertEqual(self.check().returncode, 0)
        self.assertEqual(self.failures(), 2)
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.check().returncode, 0)
        self.assertEqual(self.calls_made(), ["down", "up"])
        self.assertEqual(self.failures(), 0)
        self.assertFalse((self.state / "restore-radio0").exists())

    def test_healthy_hop_or_internet_resets_failures(self):
        for healthy in ({"HOP_OK": 1}, {"INTERNET": 1}):
            self.check()
            self.assertEqual(self.failures(), 1)
            self.assertEqual(self.check(**healthy).returncode, 0)
            self.assertEqual(self.failures(), 0)
        self.assertEqual(self.calls_made(), [])

    def test_ineligible_routes_radios_and_missing_peers_do_not_restart(self):
        for condition in ({"DEV": "wlan1-mesh_17"}, {"BABEL": 0}):
            self.check()
            self.check()
            self.assertEqual(self.check(**condition).returncode, 0)
            self.assertLess(self.failures(), 3)
            self.assertEqual(self.calls_made(), [])
            self.check(HOP_OK=1)
        for condition in (
            {"PEERS": 0},
            {"RADIO_UP": "false"}, {"IFNAME": "other"}, {"LINK_FAIL": 1},
            {"ENABLED": 0},
        ):
            self.check()
            self.check()
            self.assertEqual(self.check(**condition).returncode, 0)
            self.assertEqual(self.failures(), 0, condition)
        self.assertEqual(self.calls_made(), [])

    def test_intermittent_babel_band_switches_keep_recent_failure_evidence(self):
        self.check()
        self.check(DEV="wlan1-mesh_17")
        self.check()
        self.check(DEV="wlan1-mesh_17")
        self.assertEqual(self.failures(), 2)
        self.assertEqual(self.calls_made(), [])
        self.check()
        self.assertEqual(self.calls_made(), ["down", "up"])

    def test_old_failures_expire(self):
        self.check()
        self.check()
        (self.state / "last_failure").write_text("1\n")
        self.check()
        self.assertEqual(self.failures(), 1)
        self.assertEqual(self.calls_made(), [])

    def test_missing_peer_mapping_never_restarts(self):
        for _ in range(5):
            self.assertEqual(self.check(MAPPING=0).returncode, 0)
        self.assertEqual(self.failures(), 0)
        self.assertEqual(self.calls_made(), [])
        self.assertIn("Cannot map the Babel next hop", self.log.read_text())

    def test_cached_peer_mapping_survives_missing_neighbor_entries(self):
        self.check()
        self.check(MAPPING=0)
        self.check(MAPPING=0)
        self.assertEqual(self.calls_made(), ["down", "up"])

    def test_changing_next_hop_restarts_failure_count(self):
        self.check()
        self.check()
        self.assertEqual(self.check(HOP="10.100.1.2").returncode, 0)
        self.assertEqual(self.failures(), 1)
        self.assertEqual(self.calls_made(), [])

    def test_cooldown_and_per_boot_limit(self):
        for _ in range(3):
            self.check()
        for _ in range(4):
            self.check()
        self.assertEqual(self.calls_made(), ["down", "up"])
        (self.state / "restarts").write_text("4\n")
        (self.state / "last_restart").write_text("0\n")
        self.check()
        self.assertEqual(self.calls_made(), ["down", "up"])
        self.assertIn("Restart limit reached", self.log.read_text())

    def test_failed_radio_up_is_retried_even_if_disabled(self):
        self.check()
        self.check()
        result = self.check(UP_FAIL=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.state / "restore-radio0").exists())
        self.assertEqual(self.check(ENABLED=0).returncode, 0)
        self.assertFalse((self.state / "restore-radio0").exists())
        self.assertEqual(self.calls_made()[0], "down")
        self.assertGreaterEqual(self.calls_made().count("up"), 2)

    def test_failed_radio_down_still_attempts_radio_up(self):
        self.check()
        self.check()
        self.assertNotEqual(self.check(DOWN_FAIL=1).returncode, 0)
        self.assertEqual(self.calls_made(), ["down", "up"])
        self.assertFalse((self.state / "restore-radio0").exists())

    def test_unsupported_board_refuses_recovery(self):
        self.board.write_text("xiaomi,redmi-router-ax6000-ubootmod\n", encoding="utf-8")
        self.assertNotEqual(self.check().returncode, 0)
        self.assertEqual(self.calls_made(), [])

    def test_corrupt_state_fails_explicitly(self):
        self.check()
        (self.state / "failures").write_text("not-a-number\n")
        self.assertNotEqual(self.check().returncode, 0)
        self.assertIn("Invalid state", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
