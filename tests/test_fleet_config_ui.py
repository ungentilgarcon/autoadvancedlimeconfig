import base64
import importlib.util
import json
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

if __name__ == "__main__":
    unittest.main()
