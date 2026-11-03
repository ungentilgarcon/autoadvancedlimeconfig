import importlib.util
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript UI tests")
class PackageOnlyUiTests(unittest.TestCase):
    def test_identity_review_shows_details_and_requires_confirmation(self):
        spec = importlib.util.spec_from_file_location(
            "fleet_config_ui_identity_js", Path(__file__).resolve().parents[1] / "fleet-config-ui.py"
        )
        ui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ui)
        script = ui.PAGE.decode().split("<script>", 1)[1].split("</script>", 1)[0]
        harness = r"""
const vm = require("node:vm"), assert = require("node:assert/strict");
const script = require("node:fs").readFileSync(0, "utf8");
class Element {
  constructor(){this.listeners={};this.files=[];this.value="";this.disabled=false;this.children=[]}
  addEventListener(name,fn){this.listeners[name]=fn}
  fire(name){return this.listeners[name]?.({preventDefault(){}})}
  replaceChildren(){this.children=[]}
  append(...items){this.children.push(...items)}
}
const elements=Object.fromEntries(["form","status","dry","deploy","original","edited","packages","inventory",
 "mode","results","refresh-nodes","node-status","node-results","target-mode","targets","discover","seed",
 "firmware","firmware-check","firmware-flash","firmware-status","radio-check"].map(id=>[id,new Element()]));
elements.seed.value="root@192.0.2.1";elements.discover.checked=true;elements["target-mode"].value="all";
let requests=[],accepted=false,promptText="",answer="no";
const node={name:"old",host:"root@192.0.2.2",hostname:"new",board:"aruba,ap-303",
 model:"AP",model_id:"aruba_ap-303",source:"discovered",status:"unavailable",
 error:"SSH router hostname does not match the shared DNS record",identity_reviewable:true};
const context={
 document:{getElementById:id=>elements[id],querySelectorAll:()=>[],createElement:()=>new Element()},
 fetch:async(path,options)=>{
  requests.push({path,payload:JSON.parse(options.body)});
  let data=path==="nodes"?{nodes:[accepted?{...node,status:"available",identity_reviewable:false,identity_approved:true}:node]}:
    path==="identity-review"?{review_id:"id",advertised:"old",address:"192.0.2.2",hostname:"new",
     board:"aruba,ap-303",key_type:"ssh-ed25519",fingerprint:"SHA256:trusted"}:{approved:"old"};
  if(path==="identity-approve")accepted=true;
  return {ok:true,json:async()=>data};
 },
 prompt:message=>{promptText=message;return answer}
};
vm.runInNewContext(script,context);
(async()=>{
 await new Promise(resolve=>setImmediate(resolve));
 const getButton=()=>elements["node-results"].children[0].children[1].children.at(-1).children[0];
 await getButton().fire("click");
 assert.match(promptText,/old/);assert.match(promptText,/new/);assert.match(promptText,/SHA256:trusted/);
 assert.equal(requests.filter(r=>r.path==="identity-approve").length,0);
 answer="ACCEPT old";
 await getButton().fire("click");
 assert.equal(requests.filter(r=>r.path==="identity-approve").length,1);
 assert.equal(requests.find(r=>r.path==="identity-approve").payload.confirmation,"ACCEPT old");
 assert.equal(elements["node-results"].children[0].children[1].children.at(-1).textContent,"Locally approved");
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        result = subprocess.run(
            ["node", "-e", harness], input=script, text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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
   "refresh-nodes", "node-status", "node-results", "target-mode", "targets", "discover", "seed",
   "firmware", "firmware-check", "firmware-flash", "firmware-status", "radio-check"]
  .map(id => [id, new Element()]));
elements.dry.disabled = elements.deploy.disabled = true;
elements.inventory.value = "fleet-inventory.json";
elements.mode.value = "none";
elements["target-mode"].value = "all";
elements.discover.checked = true;
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
      path === "radio-check" ? {plan_id:"radio",nodes:["a"],files:["/etc/config/picot-radio-recovery"],
                                  excluded_candidates:[]} :
      path === "deploy" ? {change_id:"change", started:["a"], disabled:[]} :
      ({plan_id:"plan", nodes:["a"], package_managers:[]})};
  },
  prompt: () => "DEPLOY",
};
vm.runInNewContext(script, context);
(async () => {
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests[0].path, "nodes");
  assert.equal(requests[0].payload.discover, true);
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
  assert.equal(requests[0].payload.discover, true);
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
  await elements.dry.fire("click");
  assert.equal(elements.deploy.disabled, false);
  elements.seed.value = "root@192.0.2.1";
  elements.seed.fire("input");
  assert.equal(elements.deploy.disabled, true);
  assert.equal(elements["node-status"].textContent, "Discovery settings changed; refresh the node list.");
  await elements.dry.fire("click");
  assert.equal(requests.at(-1).payload.seed, "root@192.0.2.1");
  elements.discover.checked = false;
  elements.discover.fire("input");
  assert.equal(elements.deploy.disabled, true);
  elements.inventory.value = "";
  elements.seed.value = "";
  elements.discover.checked = true;
  const previousRequests = requests.length;
  elements["refresh-nodes"].fire("click");
  assert.equal(requests.length, previousRequests);
  assert.equal(elements["node-status"].textContent, "Enter a mesh seed SSH host or an inventory path.");
  elements.discover.checked = false;
  elements["refresh-nodes"].fire("click");
  assert.equal(requests.length, previousRequests);
  assert.equal(elements["node-status"].textContent, "Enter an inventory path.");
  elements.packages.value = "";
  elements.packages.fire("input");
  assert.equal(elements.dry.disabled, true);
  assert.equal(elements.deploy.disabled, true);
  await elements["radio-check"].fire("click");
  assert.equal(requests.at(-1).path, "radio-check");
  assert.equal(elements.deploy.disabled, false);
  assert.match(elements.status.textContent, /Existing settings are preserved/);
  elements.targets.fire("input");
  assert.equal(elements.deploy.disabled, true);
  await elements["radio-check"].fire("click");
  await elements.deploy.fire("click");
  assert.equal(requests.at(-1).path, "deploy");
  assert.equal(requests.at(-1).payload.plan_id, "radio");
  assert.match(elements.status.textContent, /Service restart succeeded on: a/);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            ["node", "-e", harness], input=script, text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_firmware_requires_pilot_and_remaining_confirmations(self):
        spec = importlib.util.spec_from_file_location(
            "fleet_config_ui_firmware_js", Path(__file__).resolve().parents[1] / "fleet-config-ui.py"
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
  ["form","status","dry","deploy","original","edited","packages","inventory","mode","results",
   "refresh-nodes","node-status","node-results","target-mode","targets","discover","seed",
   "firmware","firmware-check","firmware-flash","firmware-status","radio-check"].map(id => [id, new Element()]));
elements.inventory.value = "inventory.json"; elements.mode.value = "none";
elements.discover.checked = true; elements["target-mode"].value = "models";
elements.targets.value = "aruba_ap-303";
let requests = [], prompts = [], answers = ["FLASH PILOT", "FLASH REMAINING"];
class FileReader {
  readAsDataURL(file) { this.result = "data:application/octet-stream;base64," + file.data; this.onload(); }
}
const context = {
  document: {getElementById: id => elements[id], querySelectorAll: () => [], createElement: () => new Element()},
  FileReader,
  fetch: async (path, options) => {
    requests.push({path, payload: JSON.parse(options.body)});
    const data = path === "nodes" ? {nodes:[]} :
      path === "firmware-check" ? {plan_id:"pilot", nodes:["a","b"], pilot:"a", board:"aruba,ap-303",
                                   sha256:"abc", excluded_candidates:["unknown"]} :
      path === "firmware-pilot" ? {continue_id:"continue", pilot:"a", remaining:["b"],
                                   excluded_candidates:["unknown"]} :
      {completed:["b"]};
    return {ok:true, json: async () => data};
  },
  prompt: message => { prompts.push(message); return answers.shift(); },
};
vm.runInNewContext(script, context);
(async () => {
  await new Promise(resolve => setImmediate(resolve));
  elements.firmware.files = [{name:"firmware.itb", size:5, data:"aW1hZ2U="}];
  elements.firmware.fire("change");
  assert.equal(elements["firmware-check"].disabled, false);
  await elements["firmware-check"].fire("click");
  assert.equal(requests.at(-1).path, "firmware-check");
  assert.equal(requests.at(-1).payload.filename, "firmware.itb");
  assert.equal(requests.at(-1).payload.targets, "aruba_ap-303");
  assert.equal(elements["firmware-flash"].disabled, false);
  await elements["firmware-flash"].fire("click");
  assert.equal(requests.at(-1).path, "firmware-pilot");
  assert.equal(requests.at(-1).payload.confirmation, "FLASH PILOT");
  assert.match(prompts[0], /Pilot: a/);
  assert.equal(elements["firmware-flash"].textContent, "Flash remaining nodes");
  await elements["firmware-flash"].fire("click");
  assert.equal(requests.at(-1).path, "firmware-continue");
  assert.equal(requests.at(-1).payload.confirmation, "FLASH REMAINING");
  assert.match(prompts[1], /Remaining targets: b/);
  assert.equal(elements["firmware-flash"].disabled, true);
  await elements["firmware-check"].fire("click");
  elements.targets.value = "changed";
  elements.targets.fire("input");
  assert.equal(elements["firmware-flash"].disabled, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            ["node", "-e", harness], input=script, text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
