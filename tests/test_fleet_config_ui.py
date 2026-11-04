import base64
import importlib.util
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

from test_fleet_archive import archive

spec = importlib.util.spec_from_file_location(
    "fleet_config_ui", Path(__file__).resolve().parents[1] / "fleet-config-ui.py"
)
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), ui.make_handler("test-token"))
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def test_compare_does_not_return_file_contents(self):
        def encoded(data):
            return base64.b64encode(archive([("etc/config/a", data)])).decode()

        payload = json.dumps({
            "original": encoded(b"password=original-secret\n"),
            "edited": encoded(b"password=edited-secret\n"),
            "packages": "bmx7, lime-proto-bmx7",
        }).encode()
        request = urllib.request.Request(
            self.origin + "/test-token/compare", data=payload,
            headers={"Origin": self.origin, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            body = response.read().decode()
        result = json.loads(body)
        self.assertEqual(result["changes"][0]["path"], "/etc/config/a")
        self.assertEqual(result["packages"], ["bmx7", "lime-proto-bmx7"])
        self.assertNotIn("secret", body)

    def test_invalid_origin_rejected(self):
        request = urllib.request.Request(
            self.origin + "/test-token/compare", data=b"{}",
            headers={"Origin": "http://example.invalid", "Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 403)

    def test_new_executable_appears_with_permissions(self):
        original = base64.b64encode(archive([])).decode()
        edited = base64.b64encode(archive([
            ("usr/sbin/newtool", b"#!/bin/sh\nexit 0\n", 0o755),
            ("opt/project/tool", b"#!/bin/sh\nexit 0\n", 0o755),
        ])).decode()
        payload = json.dumps({"original": original, "edited": edited, "packages": ""}).encode()
        request = urllib.request.Request(
            self.origin + "/test-token/compare", data=payload,
            headers={"Origin": self.origin, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            changes = json.load(response)["changes"]
        self.assertEqual([change["path"] for change in changes],
                         ["/opt/project/tool", "/usr/sbin/newtool"])
        for change in changes:
            self.assertEqual(change["operation"], "add")
            self.assertIsNone(change["original_mode"])
            self.assertEqual(change["edited_mode"], "0755")

    def test_single_fix_archive_deploys_replacements_and_activates_selected_service(self):
        fix = base64.b64encode(archive([
            ("etc/init.d/example", b"#!/bin/sh\n", 0o755),
            ("usr/sbin/example", b"new", 0o755),
        ])).decode()
        payload = {"original": None, "edited": fix, "packages": "", "inventory": "",
                   "discover": True, "seed": "root@192.0.2.1", "target_mode": "all",
                   "targets": "", "mode": "none"}
        node = ui.deployment.fleet.Node({"name": "a", "host": "root@192.0.2.1",
                                         "mesh_ip": "192.0.2.1"})
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui, "resolve_nodes", return_value=([node], [])), \
             patch.object(ui.deployment.fleet, "remote",
                          return_value=subprocess.CompletedProcess([], 0, "", "")), \
             patch.object(ui.deployment, "read_node_file",
                          side_effect=lambda n, p: ui.deployment.Entry("file", b"old", 0o755)
                          if p == "usr/sbin/example" else None), \
             patch.object(ui.deployment, "deploy", return_value="web-fix") as deploy, \
             patch.object(ui.deployment, "activate_fix_services", return_value=["a: /etc/init.d/example"]) as activate:
            with self.post("compare", payload) as response:
                data = json.load(response)
            self.assertTrue(data["fix_package"])
            payload["selected"] = [item["path"] for item in data["changes"]]
            with self.post("dry-run", payload) as response:
                plan = json.load(response)
            self.assertEqual(plan["services"], ["/etc/init.d/example"])
            self.assertIn("a: modify /usr/sbin/example", plan["file_actions"])
            deploy.assert_not_called()
            activate.assert_not_called()
            with self.post("deploy", {"plan_id": plan["plan_id"], "confirmation": "DEPLOY"}) as response:
                result = json.load(response)
            self.assertEqual(result["activated"], ["a: /etc/init.d/example"])
            self.assertEqual(deploy.call_args.args[1], "none")
            activate.assert_called_once()

    def test_fix_service_activation_failure_reports_committed_change(self):
        fix = base64.b64encode(archive([("etc/init.d/example", b"script", 0o755)])).decode()
        payload = {"original": None, "edited": fix, "packages": "", "inventory": "",
                   "discover": True, "seed": "root@192.0.2.1", "target_mode": "all",
                   "targets": "", "mode": "none", "selected": ["/etc/init.d/example"]}
        node = ui.deployment.fleet.Node({"name": "a", "host": "root@192.0.2.1",
                                         "mesh_ip": "192.0.2.1"})
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui, "resolve_nodes", return_value=([node], [])), \
             patch.object(ui.deployment.fleet, "remote",
                          return_value=subprocess.CompletedProcess([], 0, "", "")), \
             patch.object(ui.deployment, "read_node_file", return_value=None), \
             patch.object(ui.deployment, "deploy", return_value="web-fix"), \
             patch.object(ui.deployment, "activate_fix_services",
                          side_effect=ui.ArchiveError("files were deployed but activation failed")):
            with self.post("dry-run", payload) as response:
                plan = json.load(response)
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post("deploy", {"plan_id": plan["plan_id"], "confirmation": "DEPLOY"})
            self.assertIn("Change ID: web-fix", error.exception.read().decode())

    def test_wrong_token_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.origin + "/wrong/")
        self.assertEqual(error.exception.code, 404)

    def post(self, action, payload):
        request = urllib.request.Request(
            self.origin + "/test-token/" + action, data=json.dumps(payload).encode(),
            headers={"Origin": self.origin, "Content-Type": "application/json"},
        )
        return urllib.request.urlopen(request)

    def test_inventory_field_uses_default_only_if_it_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory) / "fleet-inventory.json"
            with patch.object(ui, "Path", return_value=inventory):
                with urllib.request.urlopen(self.origin + "/test-token/") as response:
                    self.assertIn(b'id="inventory" type="text" value=""', response.read())
                inventory.write_text('{"nodes":[]}', encoding="utf-8")
                with urllib.request.urlopen(self.origin + "/test-token/") as response:
                    self.assertIn(b'id="inventory" type="text" value="fleet-inventory.json"',
                                  response.read())

    def test_explicit_missing_inventory_is_reported_even_with_seed(self):
        payload = {"inventory": "missing-inventory.json", "seed": "root@192.0.2.1",
                   "discover": True}
        with patch.object(ui.deployment.fleet, "load_passwords"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post("nodes", payload)
        self.assertIn("missing-inventory.json", error.exception.read().decode())

    def test_identity_review_requires_confirmation_and_rechecks_before_saving(self):
        approval = ui.identity.Approval("old", "192.0.2.2", "new", "aruba,ap-303",
                                         "ssh-ed25519", "SHA256:trusted")
        node = {"source": "discovered", "name": "old", "host": "root@192.0.2.2",
                "status": "unavailable", "identity_reviewable": True, "hostname": "new",
                "board": "aruba,ap-303"}
        payload = {"inventory": "", "discover": True, "seed": "root@192.0.2.1",
                   "advertised": "old", "host": "root@192.0.2.2"}
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui, "resolve_nodes", return_value=([], [node])) as resolve, \
             patch.object(ui.identity, "identify", return_value=approval) as identify, \
             patch.object(ui.identity, "save") as save:
            with self.post("identity-review", payload) as response:
                review = json.load(response)
            self.assertEqual(review["fingerprint"], "SHA256:trusted")
            save.assert_not_called()
            with self.assertRaises(urllib.error.HTTPError):
                self.post("identity-approve", {"review_id": review["review_id"], "confirmation": "yes"})
            save.assert_not_called()
            with self.post("identity-review", payload) as response:
                review = json.load(response)
            with self.post("identity-approve", {"review_id": review["review_id"],
                                                 "confirmation": "ACCEPT old"}) as response:
                self.assertEqual(json.load(response)["approved"], "old")
            self.assertEqual(resolve.call_count, 3)
            self.assertEqual(identify.call_count, 3)
            save.assert_called_once_with(approval)
            with self.assertRaises(urllib.error.HTTPError):
                self.post("identity-approve", {"review_id": review["review_id"],
                                               "confirmation": "ACCEPT old"})

    def test_identity_review_rejects_changed_or_untrusted_candidates(self):
        original = ui.identity.Approval("old", "192.0.2.2", "new", "aruba,ap-303",
                                         "ssh-ed25519", "SHA256:trusted")
        changed = ui.identity.Approval("old", "192.0.2.2", "new", "aruba,ap-303",
                                        "ssh-ed25519", "SHA256:changed")
        candidate = {"source": "discovered", "name": "old", "host": "root@192.0.2.2",
                     "status": "unavailable", "identity_reviewable": True, "hostname": "new",
                     "board": "aruba,ap-303"}
        payload = {"inventory": "", "discover": True, "seed": "root@192.0.2.1",
                   "advertised": "old", "host": "root@192.0.2.2"}
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui, "resolve_nodes", return_value=([], [candidate])), \
             patch.object(ui.identity, "identify", side_effect=[original, changed]), \
             patch.object(ui.identity, "save") as save:
            with self.post("identity-review", payload) as response:
                review = json.load(response)
            with self.assertRaises(urllib.error.HTTPError):
                self.post("identity-approve", {"review_id": review["review_id"],
                                               "confirmation": "ACCEPT old"})
            save.assert_not_called()
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui, "resolve_nodes", return_value=([], [{**candidate, "identity_reviewable": False}])), \
             patch.object(ui.identity, "identify") as identify:
            with self.assertRaises(urllib.error.HTTPError):
                self.post("identity-review", payload)
            identify.assert_not_called()

    def test_firmware_requires_pilot_and_second_confirmation(self):
        nodes = [ui.deployment.fleet.Node({"name": name, "host": f"root@192.0.2.{i}",
                                          "mesh_ip": f"192.0.2.{i}"})
                 for i, name in enumerate(("a", "b", "c"), 1)]
        image = b"firmware"
        plan = ui.firmware.FirmwarePlan(image, "image.itb", "digest", "aruba,ap-303",
                                        {node.name: node.name for node in nodes}, tuple(nodes))
        payload = {"filename": "image.itb", "image": base64.b64encode(image).decode(),
                   "inventory": "", "discover": True, "seed": "root@192.0.2.1",
                   "target_mode": "nodes", "targets": "a,b,c"}
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui, "resolve_nodes", return_value=(nodes, [])), \
             patch.object(ui.firmware, "prepare", return_value=plan) as prepare, \
             patch.object(ui.firmware, "flash_node") as flash:
            with self.post("firmware-check", payload) as response:
                checked = json.load(response)
            prepare.assert_called_once()
            self.assertEqual(checked["pilot"], "a")
            flash.assert_not_called()
            with self.assertRaises(urllib.error.HTTPError):
                self.post("firmware-pilot", {"plan_id": checked["plan_id"], "confirmation": "yes"})
            flash.assert_not_called()
            with self.post("firmware-pilot", {"plan_id": checked["plan_id"],
                                              "confirmation": "FLASH PILOT"}) as response:
                pilot = json.load(response)
            self.assertEqual([call.args[0].name for call in flash.call_args_list], ["a"])
            self.assertEqual(pilot["remaining"], ["b", "c"])
            with self.assertRaises(urllib.error.HTTPError):
                self.post("firmware-pilot", {"plan_id": checked["plan_id"],
                                             "confirmation": "FLASH PILOT"})
            with self.assertRaises(urllib.error.HTTPError):
                self.post("firmware-continue", {"plan_id": pilot["continue_id"], "confirmation": "yes"})
            with self.post("firmware-continue", {"plan_id": pilot["continue_id"],
                                                 "confirmation": "FLASH REMAINING"}) as response:
                self.assertEqual(json.load(response)["completed"], ["b", "c"])
            self.assertEqual([call.args[0].name for call in flash.call_args_list], ["a", "b", "c"])
            with self.assertRaises(urllib.error.HTTPError):
                self.post("firmware-continue", {"plan_id": pilot["continue_id"],
                                                 "confirmation": "FLASH REMAINING"})

    def test_radio_workaround_dry_run_and_confirmed_deployment(self):
        nodes = [ui.deployment.fleet.Node({"name": name, "host": f"root@192.0.2.{i}",
                                          "mesh_ip": f"192.0.2.{i}"})
                 for i, name in enumerate(("a", "b"), 1)]
        plans = [ui.deployment.NodePlan(node, ()) for node in nodes]
        payload = {"inventory": "", "discover": True, "seed": "root@192.0.2.1",
                   "target_mode": "nodes", "targets": "a,b"}
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui, "resolve_nodes", return_value=(nodes, [])), \
             patch.object(ui.radio_recovery, "prepare",
                          return_value=(plans, {"a": "a", "b": "b"})) as prepare, \
             patch.object(ui, "verify_target_boards") as verify, \
             patch.object(ui.deployment, "deploy", return_value="change") as deploy, \
             patch.object(ui.radio_recovery, "activate", return_value=(["a"], ["b"])) as activate:
            with self.post("radio-check", payload) as response:
                checked = json.load(response)
            self.assertEqual(checked["nodes"], ["a", "b"])
            self.assertEqual(len(checked["files"]), 3)
            prepare.assert_called_once()
            deploy.assert_not_called()
            activate.assert_not_called()
            with self.assertRaises(urllib.error.HTTPError):
                self.post("deploy", {"plan_id": checked["plan_id"], "confirmation": "yes"})
            with self.post("deploy", {"plan_id": checked["plan_id"], "confirmation": "DEPLOY"}) as response:
                result = json.load(response)
            self.assertEqual(result["started"], ["a"])
            self.assertEqual(result["disabled"], ["b"])
            verify.assert_called_once_with(nodes, {"a": ui.radio_recovery.BOARD,
                                                   "b": ui.radio_recovery.BOARD},
                                           {"a": "a", "b": "b"})
            deploy.assert_called_once_with(plans, "none")
            activate.assert_called_once_with(nodes)
            with self.assertRaises(urllib.error.HTTPError):
                self.post("deploy", {"plan_id": checked["plan_id"], "confirmation": "DEPLOY"})

    def test_radio_workaround_does_not_activate_when_identity_or_deployment_fails(self):
        node = ui.deployment.fleet.Node({"name": "a", "host": "root@192.0.2.1",
                                         "mesh_ip": "192.0.2.1"})
        plan = ui.deployment.NodePlan(node, ())
        payload = {"inventory": "", "discover": True, "seed": "root@192.0.2.1",
                   "target_mode": "all", "targets": ""}
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui, "resolve_nodes", return_value=([node], [])), \
             patch.object(ui.radio_recovery, "prepare", return_value=([plan], {"a": "a"})), \
             patch.object(ui, "verify_target_boards") as verify, \
             patch.object(ui.deployment, "deploy") as deploy, \
             patch.object(ui.radio_recovery, "activate") as activate:
            for failure in ("identity", "deployment"):
                with self.post("radio-check", payload) as response:
                    checked = json.load(response)
                verify.side_effect = ui.ArchiveError("changed") if failure == "identity" else None
                deploy.side_effect = ui.ArchiveError("failed") if failure == "deployment" else None
                with self.assertRaises(urllib.error.HTTPError):
                    self.post("deploy", {"plan_id": checked["plan_id"], "confirmation": "DEPLOY"})
            activate.assert_not_called()

    def test_radio_activation_failure_reports_committed_change_id(self):
        node = ui.deployment.fleet.Node({"name": "a", "host": "root@192.0.2.1",
                                         "mesh_ip": "192.0.2.1"})
        payload = {"inventory": "", "discover": True, "seed": "root@192.0.2.1",
                   "target_mode": "all", "targets": ""}
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui, "resolve_nodes", return_value=([node], [])), \
             patch.object(ui.radio_recovery, "prepare",
                          return_value=([ui.deployment.NodePlan(node, ())], {"a": "a"})), \
             patch.object(ui, "verify_target_boards"), \
             patch.object(ui.deployment, "deploy", return_value="web-example"), \
             patch.object(ui.radio_recovery, "activate",
                          side_effect=ui.ArchiveError("files were deployed but activation failed")):
            with self.post("radio-check", payload) as response:
                checked = json.load(response)
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post("deploy", {"plan_id": checked["plan_id"], "confirmation": "DEPLOY"})
            self.assertIn("Change ID: web-example", error.exception.read().decode())
            with self.assertRaises(urllib.error.HTTPError):
                self.post("deploy", {"plan_id": checked["plan_id"], "confirmation": "DEPLOY"})

    def test_firmware_failure_stops_remaining_nodes(self):
        nodes = [ui.deployment.fleet.Node({"name": name, "host": "root@192.0.2.1"})
                 for name in ("a", "b", "c")]
        plan = ui.firmware.FirmwarePlan(b"image", "image.bin", "digest", "aruba,ap-303",
                                        {node.name: node.name for node in nodes}, tuple(nodes))
        payload = {"filename": "image.bin", "image": "aW1hZ2U=", "inventory": "",
                   "target_mode": "all", "targets": ""}
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui, "resolve_nodes", return_value=(nodes, [])), \
             patch.object(ui.firmware, "prepare", return_value=plan), \
             patch.object(ui.firmware, "flash_node", side_effect=[None, ui.ArchiveError("unhealthy")]) as flash:
            with self.post("firmware-check", payload) as response:
                checked = json.load(response)
            with self.post("firmware-pilot", {"plan_id": checked["plan_id"],
                                              "confirmation": "FLASH PILOT"}) as response:
                continuation = json.load(response)["continue_id"]
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post("firmware-continue", {"plan_id": continuation, "confirmation": "FLASH REMAINING"})
            self.assertIn("Not attempted: c", error.exception.read().decode())
            self.assertEqual([call.args[0].name for call in flash.call_args_list], ["a", "b"])
            with self.assertRaises(urllib.error.HTTPError):
                self.post("firmware-continue", {"plan_id": continuation, "confirmation": "FLASH REMAINING"})

    def test_deploy_requires_dry_run_and_explicit_confirmation(self):
        original = base64.b64encode(archive([("etc/config/a", b"old\n")])).decode()
        edited = base64.b64encode(archive([("etc/config/a", b"new\n")])).decode()
        node = ui.deployment.fleet.Node({
            "name": "a", "host": "root@192.0.2.1", "mesh_ip": "192.0.2.1",
        })
        payload = {"original": original, "edited": edited, "packages": "",
                   "selected": ["/etc/config/a"], "inventory": "inventory.json", "mode": "none"}
        plan = ui.deployment.NodePlan(node, ())
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui.deployment.fleet, "load_inventory", return_value=([node], {})), \
             patch.object(ui.deployment, "preflight", return_value=[plan]) as preflight, \
             patch.object(ui.deployment, "deploy", return_value="web-id") as deploy:
            with self.post("dry-run", payload) as response:
                plan_id = json.load(response)["plan_id"]
            preflight.assert_called_once()
            deploy.assert_not_called()
            with self.assertRaises(urllib.error.HTTPError):
                self.post("deploy", {"plan_id": plan_id, "confirmation": "yes"})
            deploy.assert_not_called()
            with self.post("deploy", {"plan_id": plan_id, "confirmation": "DEPLOY"}) as response:
                self.assertEqual(json.load(response)["change_id"], "web-id")
            deploy.assert_called_once_with([plan], "none")
            with self.assertRaises(urllib.error.HTTPError):
                self.post("deploy", {"plan_id": plan_id, "confirmation": "DEPLOY"})

    def test_package_only_dry_run_and_confirmation(self):
        node = ui.deployment.fleet.Node({
            "name": "a", "host": "root@192.0.2.1", "mesh_ip": "192.0.2.1",
        })
        plan = ui.deployment.NodePlan(node, (), ("nano",), "apk")
        payload = {"original": None, "edited": None, "packages": "nano",
                   "selected": [], "inventory": "inventory.json", "mode": "none"}
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui.deployment.fleet, "load_inventory", return_value=([node], {})), \
             patch.object(ui.deployment, "preflight", return_value=[plan]) as preflight, \
             patch.object(ui.deployment, "deploy", return_value="web-id") as deploy:
            with self.post("dry-run", payload) as response:
                data = json.load(response)
            self.assertEqual(data["package_managers"], ["a: apk"])
            preflight.assert_called_once_with([node], [], ("nano",))
            deploy.assert_not_called()
            with self.post("deploy", {"plan_id": data["plan_id"], "confirmation": "DEPLOY"}) as response:
                self.assertEqual(json.load(response)["change_id"], "web-id")
            deploy.assert_called_once_with([plan], "none")

    def test_package_only_requires_valid_names(self):
        payload = {"original": None, "edited": None, "packages": "nano; reboot",
                   "selected": [], "inventory": "inventory.json", "mode": "none"}
        with patch.object(ui.deployment, "preflight") as preflight:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post("dry-run", payload)
            self.assertEqual(error.exception.code, 400)
            preflight.assert_not_called()

    def test_inventory_lists_live_models_and_unavailable_nodes(self):
        nodes = [
            ui.deployment.fleet.Node({"name": "a", "host": "root@192.0.2.1"}),
            ui.deployment.fleet.Node({"name": "b", "host": "root@192.0.2.2"}),
        ]

        def remote(node, command, timeout):
            self.assertEqual(command, "ubus call system board")
            if node.name == "a":
                return subprocess.CompletedProcess([], 0, json.dumps({
                    "hostname": "ap-a", "board_name": "aruba,ap-303", "model": "Aruba AP-303",
                }), "")
            return subprocess.CompletedProcess([], 255, "", "host unreachable")

        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui.deployment.fleet, "load_inventory", return_value=(nodes, {})), \
             patch.object(ui.deployment.fleet, "remote", side_effect=remote):
            with self.post("nodes", {"inventory": "inventory.json"}) as response:
                details = json.load(response)["nodes"]
        self.assertEqual([item["name"] for item in details], ["a", "b"])
        self.assertEqual(details[0]["status"], "available")
        self.assertEqual(details[0]["model"], "Aruba AP-303")
        self.assertEqual(details[0]["model_id"], "aruba_ap-303")
        self.assertEqual(details[0]["board"], "aruba,ap-303")
        self.assertEqual(details[1]["status"], "unavailable")
        self.assertIn("host unreachable", details[1]["error"])
        self.assertIsNone(details[1]["model_id"])

    def test_node_selection_limits_preflight_and_deploy(self):
        nodes = [
            ui.deployment.fleet.Node({"name": name, "host": f"root@192.0.2.{i}", "mesh_ip": f"192.0.2.{i}"})
            for i, name in enumerate(("a", "b", "c"), 1)
        ]
        plans = [ui.deployment.NodePlan(node, (), ("nano",), "apk") for node in (nodes[0], nodes[2])]
        payload = {"original": None, "edited": None, "packages": "nano", "selected": [],
                   "inventory": "inventory.json", "mode": "none",
                   "target_mode": "nodes", "targets": "a, c"}
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui.deployment.fleet, "load_inventory", return_value=(nodes, {})), \
             patch.object(ui.deployment, "preflight", return_value=plans) as preflight, \
             patch.object(ui.deployment, "deploy", return_value="web-id") as deploy:
            with self.post("dry-run", payload) as response:
                result = json.load(response)
            self.assertEqual(result["nodes"], ["a", "c"])
            preflight.assert_called_once_with([nodes[0], nodes[2]], [], ("nano",))
            with self.post("deploy", {"plan_id": result["plan_id"], "confirmation": "DEPLOY"}):
                pass
            deploy.assert_called_once_with(plans, "none")

    def test_model_selection_uses_live_board_and_revalidates_before_deploy(self):
        nodes = [
            ui.deployment.fleet.Node({"name": "a", "host": "root@192.0.2.1", "device": "wrong"}),
            ui.deployment.fleet.Node({"name": "b", "host": "root@192.0.2.2", "device": "aruba_ap-303"}),
            ui.deployment.fleet.Node({"name": "c", "host": "root@192.0.2.3"}),
        ]
        boards = {"a": "aruba,ap-303", "b": "xiaomi,redmi-router-ax6000-ubootmod",
                  "c": "aruba,ap-303"}

        def remote(node, command, timeout):
            return subprocess.CompletedProcess([], 0, json.dumps({
                "model": "Live model", "board_name": boards[node.name],
            }), "")

        payload = {"original": None, "edited": None, "packages": "nano", "selected": [],
                   "inventory": "inventory.json", "mode": "none",
                   "target_mode": "models", "targets": "aruba_ap-303"}
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui.deployment.fleet, "load_inventory", return_value=(nodes, {})), \
             patch.object(ui.deployment.fleet, "remote", side_effect=remote), \
             patch.object(ui.deployment, "preflight", side_effect=lambda chosen, *_: [
                 ui.deployment.NodePlan(node, (), ("nano",), "apk") for node in chosen
             ]) as preflight, \
             patch.object(ui.deployment, "deploy", return_value="web-id") as deploy:
            with self.post("dry-run", payload) as response:
                result = json.load(response)
            self.assertEqual(result["nodes"], ["a", "c"])
            preflight.assert_called_once_with([nodes[0], nodes[2]], [], ("nano",))
            boards["c"] = "different,model"
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post("deploy", {"plan_id": result["plan_id"], "confirmation": "DEPLOY"})
            self.assertIn("Cannot verify selected model", error.exception.read().decode())
            deploy.assert_not_called()
            with self.post("dry-run", payload) as response:
                result = json.load(response)
            boards["c"] = "aruba,ap-303"
            # The second plan selected only a; c is not silently added after the dry run.
            with self.post("deploy", {"plan_id": result["plan_id"], "confirmation": "DEPLOY"}):
                pass
            self.assertEqual([plan.node.name for plan in deploy.call_args.args[0]], ["a"])

    def test_invalid_and_incomplete_selection_never_preflights(self):
        nodes = [
            ui.deployment.fleet.Node({"name": "a", "host": "root@192.0.2.1"}),
            ui.deployment.fleet.Node({"name": "b", "host": "root@192.0.2.2"}),
        ]
        payload = {"original": None, "edited": None, "packages": "nano", "selected": [],
                   "inventory": "inventory.json", "mode": "none"}
        details = [
            {"name": "a", "status": "available", "model_id": "aruba_ap-303", "board": "aruba,ap-303"},
            {"name": "b", "status": "unavailable"},
        ]
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui.deployment.fleet, "load_inventory", return_value=(nodes, {})), \
             patch.object(ui, "inspect_inventory", return_value=details), \
             patch.object(ui.deployment, "preflight") as preflight:
            for mode, targets in (("nodes", ""), ("nodes", "a,missing"),
                                  ("nodes", "a,a"), ("nodes", "a,"),
                                  ("models", "aruba_ap-303"), ("all", "a"),
                                  ("invalid", "a")):
                with self.subTest(mode=mode, targets=targets):
                    with self.assertRaises(urllib.error.HTTPError):
                        self.post("dry-run", {**payload, "target_mode": mode, "targets": targets})
            preflight.assert_not_called()

    def test_model_selection_rejects_unknown_and_ambiguous_ids(self):
        nodes = [ui.deployment.fleet.Node({"name": name, "host": "root@192.0.2.1"}) for name in ("a", "b")]
        details = [
            {"name": "a", "status": "available", "model_id": "test_board", "board": "test,board"},
            {"name": "b", "status": "available", "model_id": "test_board", "board": "test_board"},
        ]
        with patch.object(ui, "inspect_inventory", return_value=details):
            with self.assertRaisesRegex(ui.ArchiveError, "Unknown model"):
                ui.select_targets(nodes, "models", "missing")
            with self.assertRaisesRegex(ui.ArchiveError, "Ambiguous model"):
                ui.select_targets(nodes, "models", "test_board")

    def test_multiple_model_ids_select_only_matching_nodes(self):
        nodes = [ui.deployment.fleet.Node({"name": name, "host": "root@192.0.2.1"})
                 for name in ("a", "b", "c", "d")]
        details = [
            {"name": name, "status": "available", "model_id": model, "board": board}
            for name, model, board in (
                ("a", "aruba_ap-303", "aruba,ap-303"),
                ("b", "other_router", "other,router"),
                ("c", "xiaomi_ax6000", "xiaomi,ax6000"),
                ("d", "aruba_ap-303", "aruba,ap-303"),
            )
        ]
        with patch.object(ui, "inspect_inventory", return_value=details):
            selected, expected = ui.select_targets(nodes, "models", "aruba_ap-303, xiaomi_ax6000")
        self.assertEqual([node.name for node in selected], ["a", "c", "d"])
        self.assertEqual(expected, {"a": "aruba,ap-303", "c": "xiaomi,ax6000", "d": "aruba,ap-303"})

    def test_selected_file_and_package_use_only_selected_node(self):
        nodes = [ui.deployment.fleet.Node({"name": name, "host": f"root@192.0.2.{i}"})
                 for i, name in enumerate(("a", "b"), 1)]
        original = base64.b64encode(archive([("etc/config/a", b"old\n")])).decode()
        edited = base64.b64encode(archive([("etc/config/a", b"new\n")])).decode()
        payload = {"original": original, "edited": edited, "packages": "nano",
                   "selected": ["/etc/config/a"], "inventory": "inventory.json", "mode": "none",
                   "target_mode": "nodes", "targets": "b"}
        plan = ui.deployment.NodePlan(nodes[1], (), ("nano",), "apk")
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui.deployment.fleet, "load_inventory", return_value=(nodes, {})), \
             patch.object(ui.deployment, "preflight", return_value=[plan]) as preflight:
            with self.post("dry-run", payload) as response:
                self.assertEqual(json.load(response)["nodes"], ["b"])
            chosen, changes, packages = preflight.call_args.args
            self.assertEqual(chosen, [nodes[1]])
            self.assertEqual([change.path for change in changes], ["etc/config/a"])
            self.assertEqual(packages, ("nano",))

    def test_live_board_inspection_surfaces_invalid_responses(self):
        node = ui.deployment.fleet.Node({"name": "a", "host": "root@192.0.2.1"})
        for stdout in ("not json", "[]", "{}", '{"model":"AP","board_name":"invalid/board"}'):
            with self.subTest(stdout=stdout), \
                 patch.object(ui.deployment.fleet, "remote",
                              return_value=subprocess.CompletedProcess([], 0, stdout, "")):
                details = ui.inspect_node(node)
                self.assertEqual(details["status"], "unavailable")
                self.assertTrue(details["error"])

    def test_model_plan_revalidation_rejects_missing_selected_nodes(self):
        node = ui.deployment.fleet.Node({"name": "a", "host": "root@192.0.2.1"})
        with patch.object(ui, "inspect_inventory") as inspect, \
             self.assertRaisesRegex(ui.ArchiveError, "no longer matches"):
            ui.verify_target_boards([node], {"a": "aruba,ap-303", "b": "aruba,ap-303"})
        inspect.assert_not_called()

    def test_discovered_nodes_are_included_and_identity_rechecked_before_deploy(self):
        seed = ui.deployment.fleet.Node({"name": "seed", "host": "root@192.0.2.1",
                                         "mesh_ip": "192.0.2.1"})
        found = ui.deployment.fleet.Node({"name": "found", "host": "root@192.0.2.2",
                                          "mesh_ip": "192.0.2.2"})
        details = [
            {"name": "seed", "source": "inventory", "status": "available",
             "board": "vendor,seed", "hostname": "seed"},
            {"name": "found", "source": "discovered", "status": "available",
             "board": "aruba,ap-303", "hostname": "found"},
            {"name": "client", "source": "discovered", "status": "unavailable", "error": "host key rejected"},
        ]
        plans = [ui.deployment.NodePlan(seed, (), ("nano",), "apk"),
                 ui.deployment.NodePlan(found, (), ("nano",), "apk")]
        payload = {"original": None, "edited": None, "packages": "nano", "selected": [],
                   "inventory": "", "seed": "root@192.0.2.1", "discover": True, "mode": "none"}
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui, "resolve_nodes", return_value=([seed, found], details)), \
             patch.object(ui.deployment, "preflight", return_value=plans) as preflight, \
             patch.object(ui.deployment, "deploy") as deploy:
            with self.post("dry-run", payload) as response:
                data = json.load(response)
            self.assertEqual(data["nodes"], ["seed", "found"])
            self.assertEqual(data["excluded_candidates"], [{"name": "client", "error": "host key rejected"}])
            self.assertEqual(preflight.call_args.args[0], [seed, found])
            changed = {"name": "found", "status": "available", "board": "aruba,ap-303",
                       "hostname": "different"}
            with patch.object(ui, "inspect_inventory", return_value=[changed]):
                with self.assertRaises(urllib.error.HTTPError):
                    self.post("deploy", {"plan_id": data["plan_id"], "confirmation": "DEPLOY"})
            deploy.assert_not_called()

    def test_approved_discovered_node_uses_live_hostname_and_rechecks_approval(self):
        found = ui.deployment.fleet.Node({"name": "old", "host": "root@192.0.2.2",
                                          "mesh_ip": "192.0.2.2"})
        found.identity_approval = ui.identity.Approval("old", "192.0.2.2", "new", "aruba,ap-303",
                                                       "ssh-ed25519", "SHA256:trusted")
        details = [{"name": "old", "source": "discovered", "status": "available",
                    "board": "aruba,ap-303", "hostname": "new", "identity_approved": True}]
        plans = [ui.deployment.NodePlan(found, (), ("nano",), "apk")]
        payload = {"original": None, "edited": None, "packages": "nano", "selected": [],
                   "inventory": "", "seed": "root@192.0.2.1", "discover": True, "mode": "none"}
        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui, "resolve_nodes", return_value=([found], details)), \
             patch.object(ui.deployment, "preflight", return_value=plans), \
             patch.object(ui, "inspect_inventory", return_value=details), \
             patch.object(ui.identity, "verify_nodes") as verify, \
             patch.object(ui.deployment, "deploy", return_value="web-id") as deploy:
            with self.post("dry-run", payload) as response:
                data = json.load(response)
            with self.post("deploy", {"plan_id": data["plan_id"], "confirmation": "DEPLOY"}) as response:
                self.assertEqual(json.load(response)["change_id"], "web-id")
            verify.assert_called_once_with(ui.deployment.fleet, [found])
            deploy.assert_called_once()

    def test_seed_only_discovery_lists_and_targets_authenticated_mesh_routers(self):
        def remote(node, command, timeout):
            if command == ui.mesh_discovery.DNS_COMMAND:
                return subprocess.CompletedProcess([], 0, "192.0.2.1 seed\n192.0.2.2 router\n", "")
            if command == ui.mesh_discovery.MESH_COMMAND:
                return subprocess.CompletedProcess([], 0, "", "")
            if command == "ubus call system board":
                name = "seed" if node.name == "mesh-seed" else node.name
                return subprocess.CompletedProcess([], 0, json.dumps({
                    "hostname": name, "model": "AP", "board_name": "aruba,ap-303",
                }), "")
            self.fail(f"Unexpected remote command: {command}")

        payload = {"original": None, "edited": None, "packages": "nano", "selected": [],
                   "inventory": "", "seed": "root@192.0.2.1", "discover": True, "mode": "none",
                   "target_mode": "models", "targets": "aruba_ap-303"}
        def preflight(nodes, changes, packages):
            return [ui.deployment.NodePlan(node, (), packages, "apk") for node in nodes]

        with patch.object(ui.deployment.fleet, "load_passwords"), \
             patch.object(ui.deployment.fleet, "remote", side_effect=remote), \
             patch.object(ui.deployment, "preflight", side_effect=preflight) as check, \
             patch.object(ui.deployment, "deploy", return_value="web-id"):
            with self.post("nodes", payload) as response:
                self.assertEqual({item["name"] for item in json.load(response)["nodes"]},
                                 {"seed", "router"})
            with self.post("dry-run", payload) as response:
                result = json.load(response)
            self.assertEqual(result["nodes"], ["router", "seed"])
            self.assertEqual([node.mesh_ip for node in check.call_args.args[0]],
                             ["192.0.2.2", "192.0.2.1"])
            with self.post("deploy", {"plan_id": result["plan_id"], "confirmation": "DEPLOY"}) as response:
                self.assertEqual(json.load(response)["change_id"], "web-id")

if __name__ == "__main__":
    unittest.main()
