#!/usr/bin/env python3
"""Local interface for reviewing and deploying OpenWrt backup changes."""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import fleet_file_deploy as deployment
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
<label>Fleet inventory <input id="inventory" type="text" value="fleet-inventory.json" required></label>
<label>Activation <select id="mode"><option value="reload">Reload configuration</option>
<option value="none">Write files only (no reload)</option></select></label>
<button type="submit">Compare backups</button></form>
<p id="status" role="status"></p><div id="results"></div>
<button id="dry" type="button" disabled>Dry run against nodes</button>
<button id="deploy" type="button" disabled>Deploy selected changes</button>
<script>
const form=document.getElementById("form"), status=document.getElementById("status");
const dry=document.getElementById("dry"), deploy=document.getElementById("deploy");
let uploaded=null, planId=null;
function invalidate(){planId=null;deploy.disabled=true}
function encoded(file){return new Promise((resolve,reject)=>{const reader=new FileReader();
reader.onload=()=>resolve(reader.result.split(",",2)[1]);reader.onerror=()=>reject(reader.error);
reader.readAsDataURL(file);});}
async function post(path,payload){const response=await fetch(path,{method:"POST",
headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
const data=await response.json();if(!response.ok)throw new Error(data.error);return data}
form.addEventListener("change",()=>{uploaded=null;dry.disabled=true;invalidate()});
form.addEventListener("submit",async event=>{event.preventDefault();status.textContent="Comparing...";
const results=document.getElementById("results");results.replaceChildren();dry.disabled=true;invalidate();
try {
const original=document.getElementById("original").files[0];
const edited=document.getElementById("edited").files[0];
if(Boolean(original)!==Boolean(edited))throw new Error("Upload both backups or neither");
if((original&&original.size>33554432)||(edited&&edited.size>33554432))
throw new Error("Each archive must be at most 32 MiB");
uploaded={original:original?await encoded(original):null,edited:edited?await encoded(edited):null,
packages:document.getElementById("packages").value};
const data=await post("compare",uploaded);
status.textContent=data.changes.length+" changed files. No routers were contacted.";
const p=document.createElement("p");p.textContent="Requested packages: "+
(data.packages.length?data.packages.join(", "):"none");results.append(p);
const table=document.createElement("table"), header=document.createElement("tr");
for(const name of ["Select","File","Operation","Original mode","Edited mode","Lines added","Lines removed"]){
const th=document.createElement("th");th.textContent=name;header.append(th)}table.append(header);
for(const change of data.changes){const row=document.createElement("tr");
const choice=document.createElement("input");choice.type="checkbox";choice.value=change.path;
choice.addEventListener("change",invalidate);const select=document.createElement("td");
select.append(choice);row.append(select);
for(const value of [change.path,change.operation,change.original_mode??"-",
change.edited_mode??"-",change.added,change.removed]){
const cell=document.createElement("td");cell.textContent=value;row.append(cell)}table.append(row)}
results.append(table);dry.disabled=!data.changes.length&&!data.packages.length;
}catch(error){status.textContent="Error: "+error.message}
});
dry.addEventListener("click",async()=>{invalidate();status.textContent="Checking every node (read-only)...";
const selected=Array.from(document.querySelectorAll("#results input:checked"),item=>item.value);
try{if(!selected.length&&!uploaded.packages.trim())throw new Error("Select files or enter packages");
const result=await post("dry-run",{...uploaded,selected,
inventory:document.getElementById("inventory").value,mode:document.getElementById("mode").value});
planId=result.plan_id;deploy.disabled=false;
status.textContent="Dry run passed for: "+result.nodes.join(", ")+". No packages or configuration were applied."+
(result.package_managers.length?" Package managers: "+result.package_managers.join(", ")+".":"");
}catch(error){status.textContent="Error: "+error.message}});
deploy.addEventListener("click",async()=>{if(!planId)return;
const answer=prompt("This will change all listed nodes and may disconnect the mesh. "+
"Packages and their dependencies are not rolled back; partial installation may remain. "+
"File rollback and synchronization are best effort. Type DEPLOY to proceed:");
if(answer!=="DEPLOY")return;const id=planId;invalidate();dry.disabled=true;
status.textContent="Deploying; keep this window open...";
try{const result=await post("deploy",{plan_id:id,confirmation:answer});
status.textContent="Deployment completed on all nodes. Change ID: "+result.change_id;
}catch(error){status.textContent="Deployment failed: "+error.message}
finally{dry.disabled=false}});
</script></body></html>"""


def decode_archive(value: object) -> bytes:
    if not isinstance(value, str) or len(value) > (MAX_ARCHIVE_BYTES + 2) // 3 * 4:
        raise ArchiveError("Invalid or oversized backup")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ArchiveError("Backup is not valid base64") from error


def make_handler(token: str):
    plans: dict[str, tuple[float, list, str]] = {}

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
            if self.path != f"/{token}/{action}" or action not in ("compare", "dry-run", "deploy"):
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
                    result = {"change_id": deployment.deploy(plan[1], plan[2])}
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
                        inventory = payload.get("inventory")
                        mode = payload.get("mode")
                        if (not isinstance(selected, list) or (not selected and not packages)
                                or any(not isinstance(path, str) for path in selected)
                                or len(set(selected)) != len(selected)
                                or not isinstance(inventory, str) or not inventory
                                or "\x00" in inventory or mode not in ("none", "reload")):
                            raise ArchiveError("Invalid dry-run options")
                        chosen = [change for change in changes if "/" + change.path in selected]
                        if len(chosen) != len(selected):
                            raise ArchiveError("Selection includes files not in the comparison")
                        deployment.fleet.load_passwords(deployment.fleet.dl.DEFAULT_PASS_FILE)
                        nodes, _ = deployment.fleet.load_inventory(Path(inventory))
                        planned = deployment.preflight(nodes, chosen, tuple(packages))
                        plan_id = secrets.token_urlsafe(32)
                        plans[plan_id] = (time.monotonic(), planned, mode)
                        result = {"plan_id": plan_id, "nodes": [node.name for node in nodes],
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
