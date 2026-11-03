import importlib.util
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript UI tests")
class PackageOnlyUiTests(unittest.TestCase):
    def test_package_input_enables_dry_run_and_invalidates_old_plan(self):
        spec = importlib.util.spec_from_file_location(
            "fleet_config_ui_js", Path(__file__).resolve().parents[1] / "fleet-config-ui.py"
        )
        ui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ui)
        script = ui.PAGE.decode().split("<script>", 1)[1].split("</script>", 1)[0]
        harness = r"""
const vm = require("node:vm");
const assert = require("node:assert/strict");
const script = require("node:fs").readFileSync(0, "utf8");
class Element {
  constructor() { this.listeners = {}; this.files = []; this.value = ""; this.disabled = false; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  fire(name) { return this.listeners[name]?.({preventDefault() {}}); }
  replaceChildren() {}
  append() {}
}
const elements = Object.fromEntries(
  ["form", "status", "dry", "deploy", "original", "edited", "packages", "inventory", "mode", "results",
   "refresh-nodes", "node-status", "node-results", "target-mode", "targets"]
  .map(id => [id, new Element()]));
elements.dry.disabled = elements.deploy.disabled = true;
elements.inventory.value = "fleet-inventory.json";
elements.mode.value = "none";
elements["target-mode"].value = "all";
let requests = [];
let delayed;
const context = {
  document: {
    getElementById: id => elements[id] || null,
    querySelectorAll: () => [],
    createElement: () => new Element(),
  },
  fetch: async (path, options) => {
    requests.push({path, payload: JSON.parse(options.body)});
    if (delayed) await delayed;
    return {ok: true, json: async () => path === "nodes" ? {nodes:[]} :
      ({plan_id:"plan", nodes:["a"], package_managers:[]})};
  },
  prompt: () => "DEPLOY",
};
vm.runInNewContext(script, context);
(async () => {
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests[0].path, "nodes");
  requests.length = 0;
  elements.packages.value = "jq";
  elements.packages.fire("input");
  assert.equal(elements.dry.disabled, false);
  assert.equal(elements.deploy.disabled, true);
  await elements.dry.fire("click");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "dry-run");
  assert.deepEqual(requests[0].payload.selected, []);
  assert.equal(requests[0].payload.original, null);
  assert.equal(requests[0].payload.edited, null);
  assert.equal(requests[0].payload.packages, "jq");
  assert.equal(requests[0].payload.target_mode, "all");
  assert.equal(elements.deploy.disabled, false);
  elements.packages.value = "jq, htop";
  elements.packages.fire("input");
  assert.equal(elements.deploy.disabled, true);
  assert.equal(elements.dry.disabled, false);
  let release;
  delayed = new Promise(resolve => { release = resolve; });
  const pending = elements.dry.fire("click");
  assert.equal(requests.at(-1).payload.packages, "jq, htop");
  elements.packages.value = "htop";
  elements.packages.fire("input");
  release();
  await pending;
  assert.equal(elements.deploy.disabled, true);
  delayed = null;
  await elements.dry.fire("click");
  assert.equal(requests.at(-1).payload.packages, "htop");
  assert.equal(elements.deploy.disabled, false);
  elements["target-mode"].value = "models";
  elements["target-mode"].fire("change");
  assert.equal(elements.deploy.disabled, true);
  assert.equal(elements.targets.disabled, false);
  elements.targets.value = "aruba_ap-303,xiaomi_redmi-router-ax6000-ubootmod";
  elements.targets.fire("input");
  await elements.dry.fire("click");
  assert.equal(requests.at(-1).payload.target_mode, "models");
  assert.equal(requests.at(-1).payload.targets, "aruba_ap-303,xiaomi_redmi-router-ax6000-ubootmod");
  assert.equal(elements.deploy.disabled, false);
  elements.targets.value = "aruba_ap-303";
  elements.targets.fire("input");
  assert.equal(elements.deploy.disabled, true);
  delayed = new Promise(resolve => { release = resolve; });
  elements["refresh-nodes"].fire("click");
  assert.equal(elements["node-status"].textContent, "Checking inventory nodes (read-only)...");
  elements.inventory.value = "another-inventory.json";
  elements.inventory.fire("input");
  release();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements["node-status"].textContent, "Inventory changed; refresh the node list.");
  delayed = null;
  elements.packages.value = "";
  elements.packages.fire("input");
  assert.equal(elements.dry.disabled, true);
  assert.equal(elements.deploy.disabled, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            ["node", "-e", harness], input=script, text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
