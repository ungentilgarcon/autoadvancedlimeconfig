import argparse
import contextlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("deploy_libremesh", ROOT / "deploy-libremesh.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)

ARUBA = {"target": "ipq40xx/generic", "profile": "aruba_ap-303", "board": "aruba,ap-303"}
REDMI = {
    "target": "mediatek/filogic",
    "profile": "xiaomi_redmi-router-ax6000-ubootmod",
    "board": "xiaomi,redmi-router-ax6000-ubootmod",
}


class ModelFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixes = self.root / "fixes"
        patcher = patch.object(deploy, "MODEL_FIXES_DIR", self.fixes)
        patcher.start()
        self.addCleanup(patcher.stop)

    def overlay(self, device, relative, content=b"test", mode=0o755):
        path = self.fixes / device["target"] / device["profile"] / "files" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(mode)
        return path

    def args(self, **overrides):
        values = dict(
            device="aruba-ap-303", target=None, version="latest", feed_branch="master",
            inject_pass=False, uci_defaults=None, dry_run=False, image_type="sysupgrade",
            output_dir=self.root / "output", imagebuilder_dir=None, build_timeout=10,
            extra_packages=[], no_default_extra_packages=False, pass_file=self.root / ".pass",
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def selector_patches(self):
        config = {
            "image_url": "https://example.invalid",
            "asu_url": "https://example.invalid",
            "libremesh": {"branches": ["master"], "flavors": {"default": []}},
            "repositories": {},
            "repository_keys": [],
        }

        def metadata(url, data=None, timeout=60):
            self.assertIsNone(data, "custom contents must not be sent to ASU")
            if url.endswith("/.versions.json"):
                return 200, {"stable_version": "25.12.6"}
            return 404, {}

        return (
            patch.object(deploy, "selector_config", return_value=config),
            patch.object(deploy, "http_json", side_effect=metadata),
            patch.object(deploy, "fetch_profile", return_value=(
                {}, {"arch_packages": "arm"}, {},
            )),
            patch.object(deploy, "asu_repositories", return_value={}),
            patch.object(deploy, "read_extra_packages", return_value=[]),
        )

    def test_selects_exact_target_and_profile(self):
        file = self.overlay(ARUBA, "usr/sbin/tool")
        self.overlay(REDMI, "opt/other")
        self.assertEqual(deploy.model_fix_files(ARUBA), [(file, Path("usr/sbin/tool"))])
        self.assertEqual(len(deploy.model_fix_files(REDMI)), 1)
        self.assertEqual(deploy.model_fix_files({**ARUBA, "target": "other/generic"}), [])

    def test_external_directory_replaces_repository_fixes(self):
        self.overlay(ARUBA, "opt/repository-fix")
        external = self.root / "private"
        private_file = external / ARUBA["target"] / ARUBA["profile"] / "files/opt/private-fix"
        private_file.parent.mkdir(parents=True)
        private_file.write_text("private", encoding="utf-8")
        with contextlib.ExitStack() as stack:
            for item in self.selector_patches():
                stack.enter_context(item)
            local = stack.enter_context(patch.object(deploy, "build_local_image"))
            deploy.build_image(self.args(fixes_dir=external))
        self.assertEqual(
            local.call_args.kwargs["fix_files"], [(private_file, Path("opt/private-fix"))]
        )

    def test_missing_external_directory_is_an_error(self):
        with contextlib.ExitStack() as stack:
            for item in self.selector_patches():
                stack.enter_context(item)
            with self.assertRaisesRegex(deploy.DeployError, "directory not found"):
                deploy.build_image(self.args(fixes_dir=self.root / "missing"))

    def test_fleet_firmware_passes_external_directory_to_builder(self):
        fleet_spec = importlib.util.spec_from_file_location(
            "fleet_libremesh", ROOT / "fleet-libremesh.py"
        )
        fleet = importlib.util.module_from_spec(fleet_spec)
        fleet_spec.loader.exec_module(fleet)
        external = self.root / "private"
        args = fleet.build_parser().parse_args([
            "firmware", "--dry-run", "--fixes-dir", str(external),
        ])
        node = fleet.Node({"name": "node", "host": "root@node", "device": "aruba-ap-303"})
        with patch.object(fleet, "load_inventory", return_value=([node], {})), \
             patch.object(fleet.dl, "build_image", return_value=None) as build:
            self.assertEqual(fleet.cmd_firmware(args), 0)
        self.assertEqual(build.call_args.args[0].fixes_dir, external)

    def test_rejects_symlinks_and_special_permissions(self):
        file = self.overlay(ARUBA, "usr/sbin/tool", mode=0o4755)
        with self.assertRaises(deploy.DeployError):
            deploy.model_fix_files(ARUBA)
        file.chmod(0o755)
        (file.parent / "link").symlink_to(file)
        with self.assertRaises(deploy.DeployError):
            deploy.model_fix_files(ARUBA)

    def test_rejects_symlinked_profile_directory(self):
        target = self.fixes / "ipq40xx" / "generic"
        target.mkdir(parents=True)
        (target / ARUBA["profile"]).symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(deploy.DeployError):
            deploy.model_fix_files(ARUBA)

    def test_matching_overlay_and_custom_defaults_force_local_build(self):
        file = self.overlay(ARUBA, "usr/sbin/tool", b"private fix")
        defaults = self.root / "defaults"
        defaults.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        with contextlib.ExitStack() as stack:
            for item in self.selector_patches():
                stack.enter_context(item)
            local = stack.enter_context(patch.object(deploy, "build_local_image"))
            deploy.build_image(self.args(uci_defaults=str(defaults)))
        self.assertEqual(local.call_args.kwargs["fix_files"], [(file, Path("usr/sbin/tool"))])
        self.assertEqual(local.call_args.kwargs["custom_defaults"], defaults.read_text())

    def test_injected_credentials_and_model_fixes_are_combined(self):
        file = self.overlay(ARUBA, "usr/sbin/tool")
        with contextlib.ExitStack() as stack:
            for item in self.selector_patches():
                stack.enter_context(item)
            stack.enter_context(patch.object(deploy, "read_equipment_pass", return_value={}))
            stack.enter_context(patch.object(deploy, "wifi_keys_from_pass", return_value=("main", "mesh")))
            stack.enter_context(patch.object(deploy, "ap_hostname_from_pass", return_value="node"))
            stack.enter_context(patch.object(deploy, "openwrt_admin_password_from_pass", return_value=""))
            local = stack.enter_context(patch.object(deploy, "build_local_image"))
            deploy.build_image(self.args(inject_pass=True))
        self.assertEqual(local.call_args.kwargs["fix_files"], [(file, Path("usr/sbin/tool"))])
        self.assertEqual(local.call_args.kwargs["main_key"], "main")
        self.assertEqual(local.call_args.kwargs["mesh_key"], "mesh")

    def test_defaults_alone_force_local_build_and_dry_run_reports_fixes(self):
        self.overlay(ARUBA, "opt/tool")
        defaults = self.root / "defaults"
        defaults.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        with contextlib.ExitStack() as stack:
            for item in self.selector_patches():
                stack.enter_context(item)
            local = stack.enter_context(patch.object(deploy, "build_local_image"))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            deploy.build_image(self.args(dry_run=True))
            self.assertIn("/opt/tool", output.getvalue())
            deploy.build_image(self.args(device="redmi-ax6000-ubootmod", uci_defaults=str(defaults)))
        self.assertEqual(local.call_args.kwargs["fix_files"], [])
        self.assertEqual(local.call_args.kwargs["custom_defaults"], defaults.read_text())

    def test_build_without_custom_files_keeps_using_asu(self):
        requests = []

        def metadata(url, data=None, timeout=60):
            if url.endswith("/.versions.json"):
                return 200, {"stable_version": "25.12.6"}
            if url.endswith("/device_packages.json"):
                return 404, {}
            requests.append(data)
            return 500, {"detail": "test failure"}

        with contextlib.ExitStack() as stack:
            for item in self.selector_patches():
                stack.enter_context(item)
            stack.enter_context(patch.object(deploy, "http_json", side_effect=metadata))
            local = stack.enter_context(patch.object(deploy, "build_local_image"))
            with self.assertRaisesRegex(deploy.DeployError, "ASU build failed"):
                deploy.build_image(self.args())
        local.assert_not_called()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["defaults"], "")

    def test_local_overlay_contents_permissions_and_metadata(self):
        source = self.overlay(ARUBA, "usr/sbin/tool", b"private fix", 0o751)
        builder = self.root / "builder"
        builder.mkdir()
        (builder / "repositories").write_text("", encoding="utf-8")
        output = self.root / "output"
        args = self.args(output_dir=output)
        captured = {}

        def make(command, **kwargs):
            files = Path(next(item[6:] for item in command if item.startswith("FILES=")))
            tool = files / "usr/sbin/tool"
            captured["data"] = tool.read_bytes()
            captured["mode"] = tool.stat().st_mode & 0o777
            captured["defaults"] = (files / "etc/uci-defaults/99-custom-defaults").read_text()
            images = builder / "bin/targets/ipq40xx/generic"
            images.mkdir(parents=True)
            (images / "openwrt-aruba_ap-303-sysupgrade.bin").write_bytes(b"test image")
            return subprocess.CompletedProcess(command, 0, "", "")

        config = {"repository_keys": ["YWJjZA=="]}
        with patch.object(deploy.shutil, "which", return_value="/usr/bin/gawk"), \
             patch.object(deploy, "local_imagebuilder_dir", return_value=(builder, "")), \
             patch.object(deploy.subprocess, "run", side_effect=make):
            image = deploy.build_local_image(
                args, config, ARUBA, "25.12.6", [], {}, "", "", "", "",
                [(source, Path("usr/sbin/tool"))], "#!/bin/sh\nexit 0\n",
            )
        self.assertEqual(captured, {
            "data": b"private fix", "mode": 0o751, "defaults": "#!/bin/sh\nexit 0\n",
        })
        metadata = json.loads(image.with_name(image.name + ".build.json").read_text())
        self.assertEqual(metadata["model_fixes"][0]["path"], "/usr/sbin/tool")
        self.assertEqual(len(metadata["model_fixes"][0]["sha256"]), 64)
        self.assertEqual(len(metadata["custom_defaults_sha256"]), 64)
        self.assertFalse(metadata["credentials_embedded"])
        self.assertEqual(image.stat().st_mode & 0o777, 0o600)

    def test_credential_files_remain_present_with_model_overlay(self):
        source = self.overlay(ARUBA, "opt/tool")
        builder = self.root / "builder"
        builder.mkdir()
        (builder / "repositories").write_text("", encoding="utf-8")
        args = self.args(inject_pass=True)
        captured = {}

        def make(command, **kwargs):
            files = Path(next(item[6:] for item in command if item.startswith("FILES=")))
            for name in ("etc/uci-defaults/90zz-lime-wifi", "usr/sbin/autoadvancedlimeconfig-configure"):
                captured[name] = (files / name).read_text()
            captured["overlay"] = (files / "opt/tool").read_bytes()
            images = builder / "bin/targets/ipq40xx/generic"
            images.mkdir(parents=True)
            (images / "openwrt-aruba_ap-303-sysupgrade.itb").write_bytes(b"test image")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(deploy.shutil, "which", return_value="/usr/bin/gawk"), \
             patch.object(deploy, "local_imagebuilder_dir", return_value=(builder, "")), \
             patch.object(deploy.subprocess, "run", side_effect=make), \
             patch.object(deploy, "build_lime_wifi_defaults", return_value="defaults"), \
             patch.object(deploy, "build_lime_wifi_recovery", return_value="recovery"):
            image = deploy.build_local_image(
                args, {"repository_keys": ["YWJjZA=="]}, ARUBA,
                "25.12.6", [], {}, "main", "mesh", "node", "",
                [(source, Path("opt/tool"))], None,
            )
        self.assertEqual(captured["etc/uci-defaults/90zz-lime-wifi"], "defaults")
        self.assertEqual(captured["usr/sbin/autoadvancedlimeconfig-configure"], "recovery")
        self.assertEqual(captured["overlay"], b"test")
        self.assertTrue(image.name.endswith("-with-pass.itb"))
        metadata = json.loads(image.with_name(image.name + ".build.json").read_text())
        self.assertTrue(metadata["credentials_embedded"])
        self.assertIsNone(metadata["custom_defaults_sha256"])

    def test_generated_defaults_collision_is_rejected(self):
        source = self.overlay(ARUBA, "etc/uci-defaults/99-custom-defaults")
        builder = self.root / "builder"
        builder.mkdir()
        (builder / "repositories").write_text("", encoding="utf-8")
        with patch.object(deploy.shutil, "which", return_value="/usr/bin/gawk"), \
             patch.object(deploy, "local_imagebuilder_dir", return_value=(builder, "")):
            with self.assertRaisesRegex(deploy.DeployError, "conflicts"):
                deploy.build_local_image(
                    self.args(), {"repository_keys": ["YWJjZA=="]}, ARUBA,
                    "25.12.6", [], {}, "", "", "", "",
                    [(source, Path("etc/uci-defaults/99-custom-defaults"))], "script",
                )

    def test_ap303_recovery_overlay_creates_boot_link_with_or_without_credentials(self):
        fix_files = deploy.model_fix_files(ARUBA, ROOT / "fixes")
        for inject_pass in (False, True):
            with self.subTest(inject_pass=inject_pass):
                builder = self.root / f"builder-{inject_pass}"
                builder.mkdir()
                (builder / "repositories").write_text("", encoding="utf-8")
                captured = {}

                def make(command, **kwargs):
                    files = Path(next(item[6:] for item in command if item.startswith("FILES=")))
                    link = files / deploy.RADIO_RECOVERY_LINK
                    captured["link"] = link.readlink().as_posix()
                    captured["script"] = (files / deploy.RADIO_RECOVERY_SCRIPT).stat().st_mode & 0o777
                    captured["config"] = (files / deploy.RADIO_RECOVERY_CONFIG).read_text()
                    if inject_pass:
                        captured["configure"] = (
                            files / "usr/sbin/autoadvancedlimeconfig-configure"
                        ).read_text()
                    images = builder / "bin/targets/ipq40xx/generic"
                    images.mkdir(parents=True)
                    (images / "openwrt-aruba_ap-303-sysupgrade.bin").write_bytes(b"image")
                    return subprocess.CompletedProcess(command, 0, "", "")

                with patch.object(deploy.shutil, "which", return_value="/usr/bin/gawk"), \
                     patch.object(deploy, "local_imagebuilder_dir", return_value=(builder, "")), \
                     patch.object(deploy.subprocess, "run", side_effect=make):
                    image = deploy.build_local_image(
                        self.args(inject_pass=inject_pass), {"repository_keys": ["YWJjZA=="]},
                        ARUBA, "25.12.6", [], {}, "main", "mesh", "node", "", fix_files, None,
                    )
                self.assertEqual(captured["link"], "../init.d/picot-radio-recovery")
                self.assertTrue(captured["script"] & 0o111)
                self.assertIn("option enabled '1'", captured["config"])
                if inject_pass:
                    self.assertIn("/etc/init.d/picot-radio-recovery enable", captured["configure"])
                    self.assertIn("/etc/init.d/picot-radio-recovery restart", captured["configure"])
                metadata = json.loads(image.with_name(image.name + ".build.json").read_text())
                self.assertIn({
                    "path": "/etc/rc.d/S99picot-radio-recovery",
                    "symlink": "../init.d/picot-radio-recovery",
                }, metadata["model_fixes"])

    def test_incomplete_recovery_overlay_is_rejected(self):
        source = self.overlay(ARUBA, str(deploy.RADIO_RECOVERY_SCRIPT))
        builder = self.root / "builder"
        builder.mkdir()
        (builder / "repositories").write_text("", encoding="utf-8")
        with patch.object(deploy.shutil, "which", return_value="/usr/bin/gawk"), \
             patch.object(deploy, "local_imagebuilder_dir", return_value=(builder, "")):
            with self.assertRaisesRegex(deploy.DeployError, "requires all"):
                deploy.build_local_image(
                    self.args(), {"repository_keys": ["YWJjZA=="]}, ARUBA,
                    "25.12.6", [], {}, "", "", "", "",
                    [(source, deploy.RADIO_RECOVERY_SCRIPT)], None,
                )

    def test_dry_run_reports_generated_radio_boot_link_only_for_ap303(self):
        with contextlib.ExitStack() as stack:
            for item in self.selector_patches():
                stack.enter_context(item)
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            deploy.build_image(self.args(dry_run=True, fixes_dir=ROOT / "fixes"))
            self.assertIn("/etc/rc.d/S99picot-radio-recovery -> ../init.d/picot-radio-recovery", output.getvalue())
            output.seek(0)
            output.truncate()
            deploy.build_image(self.args(
                dry_run=True, device="redmi-ax6000-ubootmod", fixes_dir=ROOT / "fixes",
            ))
            self.assertNotIn("picot-radio-recovery", output.getvalue())

    def test_local_combined_image_is_rejected(self):
        self.overlay(ARUBA, "opt/tool")
        with contextlib.ExitStack() as stack:
            for item in self.selector_patches():
                stack.enter_context(item)
            with self.assertRaisesRegex(deploy.DeployError, "sysupgrade"):
                deploy.build_image(self.args(image_type="combined"))


if __name__ == "__main__":
    unittest.main()
