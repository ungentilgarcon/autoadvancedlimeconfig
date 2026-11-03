#!/usr/bin/env python3
"""Local interface for reviewing and deploying OpenWrt backup changes."""

from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import fleet_file_deploy as deployment
import fleet_mesh_discovery as mesh_discovery
from fleet_archive import (
    MAX_ARCHIVE_BYTES,
    ArchiveError,
    compare,
    diff_summary,
    packages_from_text,
    read_backup,
)

PAGE = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Fleet configuration review</title>
<style>
body{font:16px system-ui,sans-serif;max-width:62rem;margin:2rem auto;padding:0 1rem}
label{display:block;margin:1rem 0}input[type=text]{width:100%;padding:.4rem;box-sizing:border-box}
button{padding:.5rem 1rem}table{border-collapse:collapse;width:100%;margin-top:1rem}
th,td{border-bottom:1px solid #bbb;text-align:left;padding:.4rem} .warning{background:#fff1c9;padding:1rem}
</style></head><body>
<h1>Fleet configuration review</h1>
<p class="warning"><strong>Experimental:</strong> deployment may disconnect the mesh.
Scheduling and confirmation are best effort, not atomic. Use a test fleet and independent
recovery access. Health checks require Babel and peer pings, not routing convergence.
The dry run does not validate UCI syntax; opkg checks package availability but not dependencies.
Packages and their side effects cannot be rolled back, even if a later step fails.</p>
<p>Upload the original OpenWrt backup and the edited backup. Only changed files under
<code>/etc</code>, <code>/usr/sbin</code> and <code>/opt</code> are shown.
File contents are not displayed.
Backups can contain secrets.</p>
<form id="form">
<label>Original .tar.gz backup (optional for package-only installs)
<input id="original" type="file" accept=".gz,.tgz"></label>
<label>Edited .tar.gz backup <input id="edited" type="file" accept=".gz,.tgz"></label>
<label>Packages to install (comma-separated)
<input id="packages" type="text" placeholder="package-one, package-two"></label>
<p>For package-only deployments, enter packages and run the dry run without uploading backups.
The node's package manager installs required dependencies from its configured repositories.</p>
<label>Fleet inventory (optional if a mesh seed is provided)
<input id="inventory" type="text" value="fleet-inventory.json"></label>
<label><input id="discover" type="checkbox" checked> Automatically discover mesh routers</label>
<label>Mesh seed SSH host (optional when inventory has a reachable seed)
<input id="seed" type="text" placeholder="root@192.0.2.1"></label>
<p>Discovery checks shared mesh DNS records, including non-router candidates. Only LibreMesh routers
authenticated by SSH and matching their advertised hostname are added to deployment targets.
Unknown SSH host keys must be verified separately.</p>
<button id="refresh-nodes" type="button">Refresh available nodes</button>
<p id="node-status" role="status"></p><div id="node-results"></div>
<label>Target selection <select id="target-mode">
<option value="all">All inventory and discovered routers</option>
<option value="models">Models</option>
<option value="nodes">Node names</option></select></label>
<label>Models or node names (comma-separated)
<input id="targets" type="text" disabled placeholder="Use model IDs or node names shown above"></label>
<label>Activation <select id="mode"><option value="reload">Reload configuration</option>
<option value="none">Write files only (no reload)</option></select></label>
<button type="submit">Compare backups</button></form>
<p id="status" role="status"></p><div id="results"></div>
<button id="dry" type="button" disabled>Dry run against nodes</button>
<button id="deploy" type="button" disabled>Deploy selected changes</button>
<script>
const form=document.getElementById("form"), status=document.getElementById("status");
const dry=document.getElementById("dry"), deploy=document.getElementById("deploy");
const originalInput=document.getElementById("original"), editedInput=document.getElementById("edited");
const packagesInput=document.getElementById("packages"), results=document.getElementById("results");
const inventoryInput=document.getElementById("inventory"), targetMode=document.getElementById("target-mode");
const discoverInput=document.getElementById("discover"), seedInput=document.getElementById("seed");
const targetsInput=document.getElementById("targets"), nodeStatus=document.getElementById("node-status");
const nodeResults=document.getElementById("node-results");
let uploaded=null, planId=null, planNodes=[], planExcluded=[], changeCount=0, revision=0, deploying=false;
let nodeRevision=0;
function invalidate(){revision++;planId=null;planNodes=[];planExcluded=[];deploy.disabled=true}
function updateDry(){dry.disabled=deploying||!uploaded||(!changeCount&&!uploaded.packages.trim())}
function updatePackageOnly(){
if(originalInput.files.length||editedInput.files.length)return false;
uploaded={original:null,edited:null,packages:packagesInput.value};changeCount=0;
results.replaceChildren();updateDry();return true}
function encoded(file){return new Promise((resolve,reject)=>{const reader=new FileReader();
reader.onload=()=>resolve(reader.result.split(",",2)[1]);reader.onerror=()=>reject(reader.error);
reader.readAsDataURL(file);});}
async function post(path,payload){const response=await fetch(path,{method:"POST",
headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
const data=await response.json();if(!response.ok)throw new Error(data.error);return data}
async function refreshNodes(){
const version=++nodeRevision, inventory=inventoryInput.value.trim();
nodeResults.replaceChildren();nodeStatus.textContent="Checking inventory nodes (read-only)...";
try{
const data=await post("nodes",{inventory,discover:discoverInput.checked,seed:seedInput.value.trim()});
if(version!==nodeRevision)return;
const table=document.createElement("table"), header=document.createElement("tr");
for(const name of ["Node","Source","SSH host","Live model","Model ID","Status"]){
const th=document.createElement("th");th.textContent=name;header.append(th)}table.append(header);
for(const node of data.nodes){const row=document.createElement("tr");
for(const value of [node.name,node.source||"inventory",node.host,node.model||"-",node.model_id||"-",
node.status==="available"?"Available":node.error]){
const cell=document.createElement("td");cell.textContent=value;row.append(cell)}table.append(row)}
nodeResults.append(table);nodeStatus.textContent=data.nodes.length+" listed; "+
data.nodes.filter(node=>node.status==="available").length+" verified or reachable. "+
"Unverified discovered candidates are excluded from deployment.";
}catch(error){if(version===nodeRevision)nodeStatus.textContent="Error: "+error.message}
}
document.getElementById("refresh-nodes").addEventListener("click",()=>{
invalidate();status.textContent="";refreshNodes()});
targetMode.addEventListener("change",()=>{
targetsInput.disabled=targetMode.value==="all";
if(targetsInput.disabled)targetsInput.value="";
invalidate();status.textContent=""});
targetsInput.addEventListener("input",()=>{invalidate();status.textContent=""});
packagesInput.addEventListener("input",()=>{invalidate();status.textContent="";
if(updatePackageOnly())return;
if(uploaded){uploaded.packages=packagesInput.value;updateDry();
const summary=document.getElementById("package-summary");
if(summary)summary.textContent="Requested packages: "+(packagesInput.value.trim()||"none")}
});
for(const input of [originalInput,editedInput])input.addEventListener("change",()=>{
invalidate();uploaded=null;changeCount=0;results.replaceChildren();status.textContent="";
if(!updatePackageOnly())updateDry()});
for(const input of [document.getElementById("mode")]){
input.addEventListener("change",()=>{invalidate();status.textContent=""})}
inventoryInput.addEventListener("input",()=>{invalidate();status.textContent="";nodeRevision++;
nodeResults.replaceChildren();nodeStatus.textContent="Inventory changed; refresh the node list."});
inventoryInput.addEventListener("change",()=>refreshNodes());
for(const input of [seedInput,discoverInput]){
input.addEventListener("input",()=>{invalidate();status.textContent="";nodeRevision++;
nodeResults.replaceChildren();nodeStatus.textContent="Discovery settings changed; refresh the node list."});
input.addEventListener("change",()=>refreshNodes())}
form.addEventListener("submit",async event=>{event.preventDefault();status.textContent="Comparing...";
results.replaceChildren();uploaded=null;changeCount=0;updateDry();invalidate();const version=revision;
try {
const original=originalInput.files[0], edited=editedInput.files[0];
if(Boolean(original)!==Boolean(edited))throw new Error("Upload both backups or neither");
if((original&&original.size>33554432)||(edited&&edited.size>33554432))
throw new Error("Each archive must be at most 32 MiB");
const payload={original:original?await encoded(original):null,edited:edited?await encoded(edited):null,
packages:packagesInput.value};
if(version!==revision)return;
const data=await post("compare",payload);
if(version!==revision)return;
uploaded=payload;changeCount=data.changes.length;
status.textContent=data.changes.length+" changed files. No routers were contacted.";
const p=document.createElement("p");p.id="package-summary";p.textContent="Requested packages: "+
(data.packages.length?data.packages.join(", "):"none");results.append(p);
const table=document.createElement("table"), header=document.createElement("tr");
for(const name of ["Select","File","Operation","Original mode","Edited mode","Lines added","Lines removed"]){
const th=document.createElement("th");th.textContent=name;header.append(th)}table.append(header);
for(const change of data.changes){const row=document.createElement("tr");
const choice=document.createElement("input");choice.type="checkbox";choice.value=change.path;
choice.addEventListener("change",()=>{invalidate();status.textContent=""});const select=document.createElement("td");
select.append(choice);row.append(select);
for(const value of [change.path,change.operation,change.original_mode??"-",
change.edited_mode??"-",change.added,change.removed]){
const cell=document.createElement("td");cell.textContent=value;row.append(cell)}table.append(row)}
results.append(table);updateDry();
}catch(error){if(version===revision)status.textContent="Error: "+error.message}
});
dry.addEventListener("click",async()=>{invalidate();status.textContent="Checking selected nodes (read-only)...";
const version=revision;
const selected=Array.from(document.querySelectorAll("#results input:checked"),item=>item.value);
try{if(!uploaded||(!selected.length&&!uploaded.packages.trim()))throw new Error("Select files or enter packages");
const result=await post("dry-run",{...uploaded,selected,
inventory:inventoryInput.value,mode:document.getElementById("mode").value,
discover:discoverInput.checked,seed:seedInput.value.trim(),
target_mode:targetMode.value,targets:targetsInput.value});
if(version!==revision)return;
planId=result.plan_id;planNodes=result.nodes;planExcluded=result.excluded_candidates||[];deploy.disabled=false;
status.textContent="Dry run passed for: "+result.nodes.join(", ")+". No packages or configuration were applied."+
(planExcluded.length?" Excluded unverified candidates: "+
planExcluded.map(item=>item.name+" ("+item.error+")").join(", ")+".":"")+
(result.package_managers.length?" Package managers: "+result.package_managers.join(", ")+".":"");
}catch(error){if(version===revision)status.textContent="Error: "+error.message}});
deploy.addEventListener("click",async()=>{if(!planId)return;
const answer=prompt("This will change: "+planNodes.join(", ")+". It may disconnect the mesh. "+
(planExcluded.length?"Unverified candidates excluded: "+planExcluded.map(item=>item.name).join(", ")+". ":"")+
"Packages and their dependencies are not rolled back; partial installation may remain. "+
"File rollback and synchronization are best effort. Type DEPLOY to proceed:");
if(answer!=="DEPLOY")return;const id=planId;invalidate();deploying=true;updateDry();
status.textContent="Deploying; keep this window open...";
try{const result=await post("deploy",{plan_id:id,confirmation:answer});
status.textContent="Deployment completed on all nodes. Change ID: "+result.change_id;
}catch(error){status.textContent="Deployment failed: "+error.message}
finally{deploying=false;updateDry()}});
refreshNodes();
</script></body></html>"""


def decode_archive(value: object) -> bytes:
    if not isinstance(value, str) or len(value) > (MAX_ARCHIVE_BYTES + 2) // 3 * 4:
        raise ArchiveError("Invalid or oversized backup")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ArchiveError("Backup is not valid base64") from error


def inventory_path(value: object) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ArchiveError("Invalid inventory path")
    return Path(value)


def inspect_node(node) -> dict:
    result = {
        "name": node.name, "host": node.host, "status": "unavailable",
        "model": None, "model_id": None, "board": None, "hostname": None,
        "error": None,
    }
    try:
        response = deployment.fleet.remote(node, "ubus call system board", timeout=20)
    except (deployment.fleet.DeployError, OSError) as error:
        result["error"] = str(error)
        return result
    if response.returncode:
        result["error"] = (response.stderr.strip() or "SSH or board query failed")[:300]
        return result
    try:
        board = json.loads(response.stdout)
    except (ValueError, TypeError):
        result["error"] = "Invalid board response"
        return result
    if not isinstance(board, dict) or not all(
        isinstance(board.get(field), str) and board[field].strip()
        for field in ("board_name", "model")
    ):
        result["error"] = "Board response is missing model or board_name"
        return result
    board_name = board["board_name"].strip()
    model_id = deployment.fleet.dl.normalize_board(board_name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", model_id):
        result["error"] = "Board response has an invalid model ID"
        return result
    hostname = board.get("hostname")
    result.update(
        status="available", model=board["model"].strip(), model_id=model_id,
        board=board_name, hostname=hostname if isinstance(hostname, str) else None,
    )
    return result


def inspect_inventory(nodes: list) -> list[dict]:
    results = deployment.fleet.parallel(nodes, inspect_node)
    return [results[node.name] for node in nodes]


def resolve_nodes(payload: dict, *, include_inventory_details: bool = True) -> tuple[list, list[dict]]:
    discover = payload.get("discover", False)
    seed = payload.get("seed", "")
    inventory = payload.get("inventory")
    if not isinstance(discover, bool) or not isinstance(seed, str) or not isinstance(inventory, str):
        raise ArchiveError("Invalid discovery options")
    if "\x00" in seed or "\x00" in inventory:
        raise ArchiveError("Invalid discovery options")
    if not discover:
        nodes, _ = deployment.fleet.load_inventory(inventory_path(inventory))
        return nodes, inspect_inventory(nodes) if include_inventory_details else []
    nodes = mesh_discovery.load_inventory(deployment.fleet, inventory.strip())
    return mesh_discovery.discover(deployment.fleet, nodes, seed.strip(), inspect_inventory)


def select_targets(nodes: list, target_mode: object, targets: object) -> tuple[list, dict[str, str]]:
    if target_mode not in ("all", "nodes", "models") or not isinstance(targets, str):
        raise ArchiveError("Invalid target selection")
    if target_mode == "all":
        if targets.strip():
            raise ArchiveError("Clear the target list when selecting all nodes")
        return nodes, {}
    values = [value.strip() for value in targets.split(",")]
    if any(not value for value in values) or len(values) != len(set(values)):
        raise ArchiveError("Enter distinct comma-separated target names or model IDs")
    if target_mode == "nodes":
        return deployment.fleet.select_nodes(nodes, values), {}

    details = inspect_inventory(nodes)
    unavailable = [item["name"] for item in details if item["status"] != "available"]
    if unavailable:
        raise ArchiveError(
            "Cannot select all matching models while inventory nodes are unavailable: "
            + ", ".join(unavailable) + ". Use node-name selection for a partial fleet."
        )
    model_boards: dict[str, set[str]] = {}
    for item in details:
        model_boards.setdefault(item["model_id"], set()).add(item["board"])
    unknown = set(values) - model_boards.keys()
    if unknown:
        raise ArchiveError("Unknown model ID(s): " + ", ".join(sorted(unknown)))
    ambiguous = [value for value in values if len(model_boards[value]) != 1]
    if ambiguous:
        raise ArchiveError("Ambiguous model ID(s): " + ", ".join(ambiguous))
    expected = {item["name"]: item["board"] for item in details if item["model_id"] in values}
    return [node for node in nodes if node.name in expected], expected


def verify_target_boards(nodes: list, expected: dict[str, str], hostnames: dict[str, str] | None = None) -> None:
    if not expected:
        return
    if not expected.keys() <= {node.name for node in nodes}:
        raise ArchiveError("Model-targeted plan no longer matches its selected nodes")
    details = inspect_inventory([node for node in nodes if node.name in expected])
    changed = [
        item["name"] for item in details
        if item["status"] != "available" or item["board"] != expected.get(item["name"])
        or (hostnames is not None and item["name"] in hostnames
            and (not isinstance(item.get("hostname"), str)
                 or item["hostname"].lower() != hostnames[item["name"]].lower()))
    ]
    if changed:
        raise ArchiveError(
            "Cannot verify selected model on: " + ", ".join(changed) + "; run a new dry run"
        )


def make_handler(token: str):
    plans: dict[str, tuple[float, list, str, dict[str, str], dict[str, str]]] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def reply(self, status: int, content: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'; "
                             "form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            if self.path != f"/{token}/":
                self.reply(404, b"Not found", "text/plain")
                return
            self.reply(200, PAGE, "text/html; charset=utf-8")

        def do_POST(self) -> None:
            action = self.path.removeprefix(f"/{token}/")
            if self.path != f"/{token}/{action}" or action not in ("nodes", "compare", "dry-run", "deploy"):
                self.reply(404, b"Not found", "text/plain")
                return
            origin = self.headers.get("Origin")
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if origin != expected_origin or self.headers.get("Content-Type") != "application/json":
                self.reply(403, b"Forbidden", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > (MAX_ARCHIVE_BYTES * 8 // 3 + 8192):
                    raise ArchiveError("Request exceeds the size limit")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ArchiveError("Invalid request")
                if action == "deploy":
                    plan_id = payload.get("plan_id")
                    if not isinstance(plan_id, str) or payload.get("confirmation") != "DEPLOY":
                        raise ArchiveError("Explicit DEPLOY confirmation is required")
                    plan = plans.pop(plan_id, None)
                    if plan is None or time.monotonic() - plan[0] > 600:
                        raise ArchiveError("Dry run expired or was already used; run it again")
                    verify_target_boards([item.node for item in plan[1]], plan[3], plan[4])
                    result = {"change_id": deployment.deploy(plan[1], plan[2])}
                elif action == "nodes":
                    deployment.fleet.load_passwords(deployment.fleet.dl.DEFAULT_PASS_FILE)
                    _, details = resolve_nodes(payload)
                    result = {"nodes": details}
                else:
                    if not isinstance(payload.get("packages"), str):
                        raise ArchiveError("Invalid comparison request")
                    original_data, edited_data = payload.get("original"), payload.get("edited")
                    if original_data is None and edited_data is None:
                        changes = []
                    elif original_data is None or edited_data is None:
                        raise ArchiveError("Upload both backups or neither")
                    else:
                        original = read_backup(decode_archive(original_data))
                        edited = read_backup(decode_archive(edited_data))
                        changes = compare(original, edited)
                    packages = packages_from_text(payload["packages"])
                    if action == "compare":
                        plans.clear()
                        items = []
                        for change in changes:
                            added, removed = diff_summary(change)
                            items.append({"path": "/" + change.path, "operation": change.operation,
                                          "original_mode": f"{change.original.mode:04o}" if change.original else None,
                                          "edited_mode": f"{change.edited.mode:04o}" if change.edited else None,
                                          "added": added, "removed": removed})
                        result = {"changes": items, "packages": packages}
                    else:
                        plans.clear()
                        selected = payload.get("selected")
                        mode = payload.get("mode")
                        if (not isinstance(selected, list) or (not selected and not packages)
                                or any(not isinstance(path, str) for path in selected)
                                or len(set(selected)) != len(selected)
                                or mode not in ("none", "reload")):
                            raise ArchiveError("Invalid dry-run options")
                        chosen = [change for change in changes if "/" + change.path in selected]
                        if len(chosen) != len(selected):
                            raise ArchiveError("Selection includes files not in the comparison")
                        deployment.fleet.load_passwords(deployment.fleet.dl.DEFAULT_PASS_FILE)
                        nodes, details = resolve_nodes(payload, include_inventory_details=False)
                        nodes, expected = select_targets(
                            nodes, payload.get("target_mode", "all"), payload.get("targets", "")
                        )
                        chosen_names = {node.name for node in nodes}
                        expected.update({
                            item["name"]: item["board"] for item in details
                            if item.get("source") == "discovered" and item["status"] == "available"
                            and item["name"] in chosen_names
                        })
                        expected_hostnames = {
                            item["name"]: item["name"] for item in details
                            if item.get("source") == "discovered" and item["status"] == "available"
                            and item["name"] in chosen_names
                        }
                        planned = deployment.preflight(nodes, chosen, tuple(packages))
                        plan_id = secrets.token_urlsafe(32)
                        plans[plan_id] = (time.monotonic(), planned, mode, expected, expected_hostnames)
                        result = {"plan_id": plan_id, "nodes": [node.name for node in nodes],
                                  "excluded_candidates": [
                                      {"name": item["name"], "error": item.get("error")}
                                      for item in details
                                      if item.get("source") == "discovered" and item["status"] != "available"
                                  ],
                                  "package_managers": [
                                      f"{plan.node.name}: {plan.package_manager}"
                                      for plan in planned if plan.package_manager
                                  ]}
                status = 200
            except (ArchiveError, deployment.fleet.DeployError, OSError,
                    ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                result = {"error": str(error)}
                status = 400
            self.reply(status, json.dumps(result).encode(), "application/json")

    return Handler


def main() -> int:
    token = secrets.token_urlsafe(32)
    server = HTTPServer(("127.0.0.1", 0), make_handler(token))
    url = f"http://127.0.0.1:{server.server_port}/{token}/"
    print(f"Fleet configuration interface: {url}", flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
