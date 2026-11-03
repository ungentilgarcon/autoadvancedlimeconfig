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
