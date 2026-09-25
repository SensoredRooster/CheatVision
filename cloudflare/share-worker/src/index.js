import { pbkdf2 } from "node:crypto";

const MAX_UPLOAD_BYTES = 75 * 1024 * 1024;
const MULTIPART_PART_BYTES = 50 * 1024 * 1024;
const MAX_MULTIPART_BYTES = 10 * 1024 * 1024 * 1024;
const SESSION_SECONDS = 12 * 60 * 60;
const AUTH_TOKEN_SECONDS = 20 * 60;
const PASSWORD_ITERATIONS = 210000;
const FOLDERS = ["Releases","Tester Uploads","VODs","Screenshots","Bug Reports","Logs","Archived"];
const TESTER_UPLOAD_FOLDERS = new Set(["Tester Uploads", "Screenshots", "Bug Reports", "Logs"]);
const META_LATEST = "__portal/latest.json";
const PORTAL_ORIGIN = "https://cheatvision-share.sensoredrooster-com.workers.dev";
const encoder = new TextEncoder();

function baseHeaders(extra = {}) {
  return {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    ...extra,
  };
}
function json(value, status = 200, extra = {}) {
  return new Response(JSON.stringify(value), { status, headers: baseHeaders({ "content-type": "application/json; charset=utf-8", ...extra }) });
}
function html(value, status = 200, extra = {}) {
  return new Response(value, { status, headers: baseHeaders({ "content-type": "text/html; charset=utf-8", ...extra }) });
}
function b64url(bytes) {
  let binary = ""; for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}
function fromB64url(value) {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, c => c.charCodeAt(0));
}
async function sha256Hex(value) {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(value)));
  return Array.from(digest, byte => byte.toString(16).padStart(2, "0")).join("");
}
function constantEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0; for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i); return diff === 0;
}
function randomToken(bytes = 32) {
  const value = new Uint8Array(bytes);
  crypto.getRandomValues(value);
  return b64url(value);
}
function pbkdf2Sha256(password, salt, iterations) {
  return new Promise((resolve, reject) => {
    pbkdf2(password, salt, iterations, 32, "sha256", (error, derivedKey) => {
      if (error) reject(error);
      else resolve(new Uint8Array(derivedKey));
    });
  });
}
async function passwordDigest(password, salt = crypto.getRandomValues(new Uint8Array(16))) {
  const bits = await pbkdf2Sha256(password, salt, PASSWORD_ITERATIONS);
  return { salt: b64url(salt), hash: b64url(bits), iterations: PASSWORD_ITERATIONS };
}
async function passwordMatches(password, user) {
  if (!user.password_salt || !user.password_hash) return false;
  const salt = fromB64url(user.password_salt);
  const bits = await pbkdf2Sha256(password, salt, Number(user.password_iterations) || PASSWORD_ITERATIONS);
  return constantEqual(b64url(bits), user.password_hash);
}
function validEmail(value) {
  const email = String(value || "").trim().toLowerCase();
  if (email.length > 254 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return "";
  return email;
}
function validPassword(value) {
  return typeof value === "string" && value.length >= 14 && value.length <= 128;
}
function sessionCookie(value, maxAge = SESSION_SECONDS) {
  return `share_session=${value}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${maxAge}`;
}
async function verifySession(request, env) {
  const cookie = request.headers.get("cookie") || "";
  const match = cookie.match(/(?:^|;\s*)share_session=([^;]+)/);
  if (!match) return null;
  try {
    const tokenHash = await sha256Hex(match[1]);
    const now = Math.floor(Date.now() / 1000);
    const user = await env.AUTH_DB.prepare(`SELECT u.user_id, u.email, u.role, u.auth_version
      FROM sessions s JOIN users u ON u.user_id=s.user_id
      WHERE s.token_hash=? AND s.expires_at>? AND s.auth_version=u.auth_version AND u.is_active=1`)
      .bind(tokenHash, now).first();
    return user ? { userId: user.user_id, email: user.email, role: user.role, tokenHash } : null;
  } catch { return null; }
}
async function createSession(env, user) {
  const raw = randomToken();
  const now = Math.floor(Date.now() / 1000);
  await env.AUTH_DB.prepare("DELETE FROM sessions WHERE expires_at<=?").bind(now).run();
  await env.AUTH_DB.prepare("INSERT INTO sessions (token_hash,user_id,auth_version,expires_at,created_at) VALUES (?,?,?,?,?)")
    .bind(await sha256Hex(raw), user.user_id, user.auth_version, now + SESSION_SECONDS, now).run();
  return raw;
}
async function adminExists(env) {
  return !!(await env.AUTH_DB.prepare("SELECT 1 AS found FROM users WHERE role='admin' LIMIT 1").first());
}
async function issueToken(env, user, purpose) {
  const raw = randomToken();
  const now = Math.floor(Date.now() / 1000);
  const hash = await sha256Hex(raw);
  await env.AUTH_DB.batch([
    env.AUTH_DB.prepare("DELETE FROM auth_tokens WHERE expires_at<=? OR used_at IS NOT NULL").bind(now),
    env.AUTH_DB.prepare("DELETE FROM auth_tokens WHERE user_id=? AND purpose=? AND used_at IS NULL").bind(user.user_id, purpose),
    env.AUTH_DB.prepare("INSERT INTO auth_tokens (token_hash,user_id,purpose,expires_at,created_at) VALUES (?,?,?,?,?)")
      .bind(hash, user.user_id, purpose, now + AUTH_TOKEN_SECONDS, now),
  ]);
  return `${PORTAL_ORIGIN}/reset?token=${encodeURIComponent(raw)}`;
}
function normalizeKey(raw) {
  let value = decodeURIComponent(String(raw || "")).replace(/\\/g, "/");
  value = value.split("/").filter(part => part && part !== "." && part !== "..").join("/");
  if (!value || value.startsWith("__portal/") || value.length > 500) return "";
  return value;
}
function topFolder(key) { return key.split("/")[0] || ""; }
function safeFilename(name) { return String(name || "download.bin").replace(/[\r\n"\\/]+/g, "_").slice(0,180); }
function canUpload(role, key) {
  const folder = topFolder(key);
  if (!FOLDERS.includes(folder)) return false;
  if (role === "admin") return true;
  if (folder === "VODs") return key.startsWith("VODs/Incoming/");
  return TESTER_UPLOAD_FOLDERS.has(folder);
}
async function readLatest(env) {
  const obj = await env.SHARE_BUCKET.get(META_LATEST); if (!obj) return null;
  try { return JSON.parse(await obj.text()); } catch { return null; }
}

const LOGIN = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CheatVision Tester Share</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,sans-serif;background:#080b12;color:#f5f7ff;min-height:100vh;display:grid;place-items:center}.card{width:min(92vw,460px);padding:28px;border-radius:22px;background:linear-gradient(180deg,#151b2a,#0e1320);border:1px solid #293246;box-shadow:0 24px 70px #0008}.eyebrow{font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:#7dd3fc;font-weight:800}h1{margin:8px 0 6px;font-size:28px}.muted{color:#9da8bc;line-height:1.5}input,button{width:100%;margin-top:12px;border-radius:12px;border:1px solid #30394e;background:#0a0f19;color:#fff;padding:12px 14px;font:inherit}button{background:#2563eb;border-color:#3b82f6;font-weight:800;cursor:pointer}.error{min-height:22px;color:#fca5a5;margin-top:10px}a{color:#7dd3fc}</style></head>
<body><main class="card"><div class="eyebrow">Private tester share</div><h1>CheatVision</h1><p class="muted">Sign in with the email and password assigned to your account. Ask the administrator for an account or reset link if needed.</p><input id="email" type="email" autocomplete="username" placeholder="Email address"><input id="password" type="password" autocomplete="current-password" placeholder="Password"><button id="login">Sign in</button><p><a href="/forgot">Forgot password?</a></p><div class="error" id="error"></div></main>
<script>document.getElementById("login").onclick=async()=>{const b=document.getElementById("login");b.disabled=true;const r=await fetch("/login",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({email:document.getElementById("email").value,password:document.getElementById("password").value})});if(r.ok)location.href="/";else{const d=await r.json().catch(()=>({}));document.getElementById("error").textContent=d.error||"Sign in failed.";b.disabled=false;}};document.getElementById("password").addEventListener("keydown",e=>{if(e.key==="Enter")document.getElementById("login").click()});</script></body></html>`;

const FORGOT = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Password reset</title><style>:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,sans-serif;background:#080b12;color:#f5f7ff;min-height:100vh;display:grid;place-items:center}.card{width:min(92vw,460px);padding:28px;border-radius:22px;background:#111827;border:1px solid #293246}a{color:#7dd3fc}.muted{color:#9da8bc;line-height:1.5}</style></head><body><main class="card"><h1>Need a password reset?</h1><p class="muted">Password resets are handled by the portal administrator. Contact them directly; they can generate a private, one-time reset link that expires in 20 minutes.</p><a href="/login">Back to sign in</a></main></body></html>`;

const RESET = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Set your password</title><style>:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,sans-serif;background:#080b12;color:#f5f7ff;min-height:100vh;display:grid;place-items:center}.card{width:min(92vw,460px);padding:28px;border-radius:22px;background:#111827;border:1px solid #293246}input,button{width:100%;margin-top:12px;border-radius:12px;border:1px solid #30394e;background:#0a0f19;color:#fff;padding:12px 14px;font:inherit}button{background:#2563eb;font-weight:800}.muted{color:#9da8bc;line-height:1.5}</style></head><body><main class="card"><h1>Set a new password</h1><p class="muted">Use at least 14 characters. This one-time link expires after 20 minutes.</p><input id="password" type="password" autocomplete="new-password" placeholder="New password (14+ characters)"><input id="confirm" type="password" autocomplete="new-password" placeholder="Confirm password"><button id="save">Save password</button><p id="message" class="muted"></p></main><script>document.getElementById("save").onclick=async()=>{const p=document.getElementById("password").value;if(p!==document.getElementById("confirm").value){document.getElementById("message").textContent="Passwords do not match.";return}const token=new URLSearchParams(location.search).get("token")||"";const r=await fetch("/auth/complete-token",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({token,password:p})});const d=await r.json().catch(()=>({}));document.getElementById("message").textContent=r.ok?"Password saved. You can now sign in.":(d.error||"This link is invalid or has expired.")}</script></body></html>`;

const SETUP = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Portal administrator setup</title><style>:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,sans-serif;background:#080b12;color:#f5f7ff;min-height:100vh;display:grid;place-items:center}.card{width:min(92vw,460px);padding:28px;border-radius:22px;background:#111827;border:1px solid #293246}input,button{width:100%;margin-top:12px;border-radius:12px;border:1px solid #30394e;background:#0a0f19;color:#fff;padding:12px 14px;font:inherit}button{background:#2563eb;font-weight:800}.muted{color:#9da8bc;line-height:1.5}</style></head><body><main class="card"><h1>Set up or recover the administrator</h1><p class="muted">Use the private setup key configured by the portal owner and a unique password with at least 14 characters. Once an admin exists, this only changes that same admin account.</p><input id="key" type="password" autocomplete="off" placeholder="Private setup key"><input id="email" type="email" autocomplete="username" placeholder="Administrator email"><input id="password" type="password" autocomplete="new-password" placeholder="New password (14+ characters)"><button id="create">Save administrator password</button><p id="message" class="muted"></p></main><script>document.getElementById("create").onclick=async()=>{const r=await fetch("/auth/bootstrap",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({setup_key:document.getElementById("key").value,email:document.getElementById("email").value,password:document.getElementById("password").value})});const d=await r.json().catch(()=>({}));if(r.ok)location.href="/login";else document.getElementById("message").textContent=d.error||"Setup failed."}</script></body></html>`;

const PORTAL = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CheatVision Tester Share</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,sans-serif;background:#070a10;color:#eef3ff}header{position:sticky;top:0;z-index:3;background:#090d16ef;backdrop-filter:blur(16px);border-bottom:1px solid #222b3d;padding:14px 20px;display:flex;align-items:center;gap:12px;justify-content:space-between}.brand strong{display:block;font-size:18px}.brand span{color:#8fa1ba;font-size:12px}.wrap{max-width:1180px;margin:auto;padding:22px}.grid{display:grid;grid-template-columns:220px 1fr;gap:18px}.panel{background:#101622;border:1px solid #263044;border-radius:18px;padding:16px}.folders button{display:block;width:100%;text-align:left;margin:4px 0;padding:10px 12px;border:0;border-radius:10px;background:transparent;color:#cbd5e1;cursor:pointer}.folders button.active{background:#1d4ed8;color:#fff}.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}button,input{border-radius:10px;border:1px solid #334057;background:#0a101b;color:#fff;padding:9px 11px;font:inherit}button{cursor:pointer}.primary{background:#2563eb;border-color:#3b82f6;font-weight:750}.danger{background:#7f1d1d;border-color:#b91c1c}.files{width:100%;border-collapse:collapse}.files th,.files td{padding:10px 8px;border-bottom:1px solid #202a3d;text-align:left;font-size:13px}.files th{color:#94a3b8}.files a{color:#7dd3fc;text-decoration:none}.latest{margin-bottom:14px;padding:14px;border:1px solid #245a9c;border-radius:14px;background:#0d2441}.latest a{color:#93c5fd}.muted{color:#8fa1ba}.progress{height:7px;background:#1f2937;border-radius:999px;overflow:hidden;margin-top:8px}.progress i{display:block;height:100%;background:#38bdf8;width:0}.link-box{display:flex;gap:8px;margin-top:12px}.link-box input{flex:1;min-width:0}.hidden{display:none!important}@media(max-width:760px){.grid{grid-template-columns:1fr}.folders{display:flex;overflow:auto;gap:5px}.folders button{white-space:nowrap;width:auto}.files th:nth-child(3),.files td:nth-child(3){display:none}.link-box{flex-direction:column}}</style></head>
<body><header><div class="brand"><strong>CheatVision Tester Share</strong><span id="role"></span></div><button id="logout">Sign out</button></header><div class="wrap"><div id="latest" class="latest hidden"></div><div class="grid"><aside class="panel folders" id="folders"></aside><section class="panel"><div class="toolbar"><input id="file" type="file"><input id="desc" placeholder="Optional description"><button class="primary" id="upload">Upload</button><span class="muted" id="status"></span></div><div class="progress hidden" id="progress"><i></i></div><table class="files"><thead><tr><th>Name</th><th>Size</th><th>Uploaded</th><th>Description</th><th>Actions</th></tr></thead><tbody id="rows"></tbody></table></section></div><section id="admin" class="panel hidden" style="margin-top:18px"><h2>Tester accounts</h2><p class="muted">Create accounts and privately share the one-time setup link. Reset links expire in 20 minutes and work once.</p><div class="toolbar"><input id="inviteEmail" type="email" placeholder="Tester email address"><button class="primary" id="inviteUser">Create tester account</button><span id="accountStatus" class="muted"></span></div><table class="files"><thead><tr><th>Email</th><th>Status</th><th>Created</th><th>Action</th></tr></thead><tbody id="users"></tbody></table><div id="linkResult" class="hidden"><p id="linkMessage" class="muted"></p><div class="link-box"><input id="oneTimeLink" readonly><button id="copyLink">Copy one-time link</button></div></div></section></div>
<script>let role="tester",folder="Releases";const folders=["Releases","Tester Uploads","VODs/Incoming","VODs/Reviewed","VODs/Archived","Screenshots","Bug Reports","Logs","Archived"];const fmt=n=>n<1024?n+" B":n<1048576?(n/1024).toFixed(1)+" KB":n<1073741824?(n/1048576).toFixed(1)+" MB":(n/1073741824).toFixed(1)+" GB";async function api(url,opt){const r=await fetch(url,opt);if(r.status===401){location.href="/login";throw new Error("Unauthorized")}const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||("HTTP "+r.status));return d}
async function init(){const me=await api("/api/me");role=me.role;document.getElementById("role").textContent=(role==="admin"?"Administrator":"Tester")+" · "+me.email;const box=document.getElementById("folders");folders.forEach(f=>{const b=document.createElement("button");b.textContent=f;b.onclick=()=>selectFolder(f);b.dataset.folder=f;box.appendChild(b)});await selectFolder("Releases");await loadLatest();if(role==="admin"){document.getElementById("admin").classList.remove("hidden");await loadUsers()}}
async function selectFolder(f){folder=f;document.querySelectorAll("[data-folder]").forEach(b=>b.classList.toggle("active",b.dataset.folder===f));const can=role==="admin"||["Tester Uploads","VODs/Incoming","Screenshots","Bug Reports","Logs"].includes(f);document.getElementById("file").disabled=!can;document.getElementById("desc").disabled=!can;document.getElementById("upload").disabled=!can;const d=await api("/api/list?prefix="+encodeURIComponent(f+"/"));const rows=document.getElementById("rows");rows.innerHTML="";d.objects.forEach(o=>{const tr=document.createElement("tr");const name=o.key.slice((f+"/").length);const actions=['<a href="/file/'+encodeURIComponent(o.key)+'">Download</a>'];if(role==="admin"){if(f==="Releases")actions.push('<button data-latest="'+encodeURIComponent(o.key)+'">Latest</button>');actions.push('<button class="danger" data-delete="'+encodeURIComponent(o.key)+'">Delete</button>')}tr.innerHTML='<td>'+escapeHtml(name)+'</td><td>'+fmt(o.size)+'</td><td>'+new Date(o.uploaded).toLocaleString()+'</td><td>'+escapeHtml(o.description||"")+'</td><td>'+actions.join(" ")+'</td>';rows.appendChild(tr)});rows.querySelectorAll("[data-delete]").forEach(b=>b.onclick=()=>removeFile(decodeURIComponent(b.dataset.delete)));rows.querySelectorAll("[data-latest]").forEach(b=>b.onclick=()=>markLatest(decodeURIComponent(b.dataset.latest)))}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]))}
function showOneTimeLink(message,url){document.getElementById("linkMessage").textContent=message;document.getElementById("oneTimeLink").value=url;document.getElementById("linkResult").classList.remove("hidden");document.getElementById("linkResult").scrollIntoView({behavior:"smooth",block:"nearest"})}
async function loadUsers(){const d=await api("/api/users");const rows=document.getElementById("users");rows.innerHTML="";d.users.forEach(u=>{const tr=document.createElement("tr");let action="Administrator";if(u.role==="tester"){action='<button data-reset="'+encodeURIComponent(u.user_id)+'">'+(u.active?"Generate reset link":"Generate setup link")+'</button>'+(u.active?' <button class="danger" data-disable="'+encodeURIComponent(u.user_id)+'">Disable</button>':"")}tr.innerHTML='<td>'+escapeHtml(u.email)+'</td><td>'+(u.active?"Active":"Disabled / setup pending")+'</td><td>'+new Date(u.created_at*1000).toLocaleDateString()+'</td><td>'+action+'</td>';rows.appendChild(tr)});rows.querySelectorAll("[data-reset]").forEach(b=>b.onclick=async()=>{try{const d=await api("/api/users/reset-link",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({user_id:decodeURIComponent(b.dataset.reset)})});showOneTimeLink("Privately share this one-time "+(d.purpose==="invite"?"setup":"reset")+" link with "+d.email+". It expires in 20 minutes.",d.url)}catch(e){document.getElementById("accountStatus").textContent=e.message}});rows.querySelectorAll("[data-disable]").forEach(b=>b.onclick=async()=>{if(!confirm("Disable this tester's access? Their files will remain."))return;try{await api("/api/users/disable",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({user_id:decodeURIComponent(b.dataset.disable)})});await loadUsers()}catch(e){document.getElementById("accountStatus").textContent=e.message}})}
document.getElementById("inviteUser").onclick=async()=>{const button=document.getElementById("inviteUser");button.disabled=true;try{const d=await api("/api/users/invite",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({email:document.getElementById("inviteEmail").value})});document.getElementById("inviteEmail").value="";showOneTimeLink("Privately share this one-time setup link with "+d.email+". It expires in 20 minutes.",d.url);document.getElementById("accountStatus").textContent="Tester account created.";await loadUsers()}catch(e){document.getElementById("accountStatus").textContent=e.message}finally{button.disabled=false}};
document.getElementById("copyLink").onclick=async()=>{try{await navigator.clipboard.writeText(document.getElementById("oneTimeLink").value);document.getElementById("linkMessage").textContent+=" Link copied."}catch{document.getElementById("oneTimeLink").select();document.getElementById("linkMessage").textContent+=" Select and copy the link."}};
async function multipartUpload(file,key,description,bar){let uploadId="";try{const start=await api("/api/multipart/start",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({key,description,content_type:file.type||"application/octet-stream",size:file.size})});uploadId=start.upload_id;const partSize=start.part_size,parts=[];let offset=0,partNumber=1;while(offset<file.size){const end=Math.min(offset+partSize,file.size);const r=await fetch("/api/multipart/part?key="+encodeURIComponent(key)+"&upload_id="+encodeURIComponent(uploadId)+"&part="+partNumber,{method:"POST",headers:{"content-type":"application/octet-stream"},body:file.slice(offset,end)});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||("Upload part "+partNumber+" failed"));parts.push({partNumber:d.partNumber,etag:d.etag});offset=end;partNumber++;bar.style.width=Math.max(5,Math.round((offset/file.size)*95))+"%";document.getElementById("status").textContent="Uploading "+file.name+" · "+Math.round((offset/file.size)*100)+"%"}await api("/api/multipart/complete",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({key,upload_id:uploadId,parts})});bar.style.width="100%"}catch(e){if(uploadId)await api("/api/multipart/abort",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({key,upload_id:uploadId})}).catch(()=>{});throw e}}
document.getElementById("upload").onclick=async()=>{const input=document.getElementById("file"),file=input.files[0];if(!file)return;const key=folder+"/"+file.name,description=document.getElementById("desc").value||"";const p=document.getElementById("progress"),bar=p.querySelector("i");p.classList.remove("hidden");bar.style.width="5%";try{if(file.size>50*1024*1024){await multipartUpload(file,key,description,bar)}else{const r=await fetch("/api/upload",{method:"POST",headers:{"x-file-path":encodeURIComponent(key),"x-description":encodeURIComponent(description),"content-type":file.type||"application/octet-stream"},body:file});if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.error||"Upload failed")}bar.style.width="100%"}document.getElementById("status").textContent="Uploaded "+file.name;input.value="";document.getElementById("desc").value="";await selectFolder(folder)}catch(e){document.getElementById("status").textContent=e.message}finally{setTimeout(()=>p.classList.add("hidden"),900)}};
async function removeFile(key){if(!confirm("Delete this file?"))return;await api("/api/file?key="+encodeURIComponent(key),{method:"DELETE"});await selectFolder(folder);await loadLatest()}async function markLatest(key){await api("/api/latest",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({key})});await loadLatest()}async function loadLatest(){const d=await api("/api/latest");const box=document.getElementById("latest");if(!d.latest){box.classList.add("hidden");return}box.classList.remove("hidden");box.innerHTML='<strong>Latest test build</strong><br><a href="/file/'+encodeURIComponent(d.latest.key)+'">'+escapeHtml(d.latest.key.replace("Releases/",""))+'</a><span class="muted"> · marked '+new Date(d.latest.marked_at).toLocaleString()+'</span>'}document.getElementById("logout").onclick=async()=>{await fetch("/logout",{method:"POST"});location.href="/login"};init().catch(e=>document.getElementById("status").textContent=e.message);</script></body></html>`;

export default { async fetch(request, env) {
  const url = new URL(request.url);
  const ip = request.headers.get("cf-connecting-ip") || "unknown";
  try {
    if (request.method === "GET" && url.pathname === "/health") {
      await env.AUTH_DB.prepare("SELECT 1 AS ready").first();
      return json({ ok: true, service: "cheatvision-tester-share", auth: "d1", storage: "r2" });
    }
    if (request.method === "GET" && url.pathname === "/login") return html(LOGIN);
    if (request.method === "GET" && url.pathname === "/forgot") return html(FORGOT);
    if (request.method === "GET" && url.pathname === "/reset") return html(RESET);
    if (request.method === "GET" && url.pathname === "/setup") return html(SETUP);

    if (request.method === "POST" && url.pathname === "/auth/bootstrap") {
      try {
        const rate = await env.AUTH_RATE_LIMITER.limit({ key: "bootstrap:" + ip });
        if (!rate.success) return json({ error: "Please wait before trying again." }, 429);
      } catch (error) {
        // The setup key still protects this route. Do not make first-time admin
        // setup impossible if the optional rate-limit binding is temporarily unhealthy.
        console.error("Bootstrap rate limiter failed", error?.message || "unknown error");
      }
      let body;
      try { body = await request.json(); } catch { return json({ error: "Invalid request." }, 400); }
      const setupSecret = String(env.BOOTSTRAP_SECRET || "");
      if (!setupSecret) return json({ error: "Administrator setup is not configured yet." }, 503);
      if (!constantEqual(String(body?.setup_key || ""), setupSecret)) return json({ error: "Invalid setup key." }, 403);
      const email = validEmail(body?.email);
      const password = String(body?.password || "");
      if (!email) return json({ error: "Enter a valid email address." }, 400);
      if (!validPassword(password)) return json({ error: "Choose a password between 14 and 128 characters." }, 400);

      let digest;
      try {
        digest = await passwordDigest(password);
      } catch (error) {
        console.error("Bootstrap password hashing failed", error?.message || "unknown error");
        return json({ error: "Administrator password setup failed (bootstrap-crypto)." }, 500);
      }

      let existingAdmin;
      try {
        existingAdmin = await env.AUTH_DB.prepare("SELECT user_id,email FROM users WHERE role='admin' LIMIT 1").first();
      } catch (error) {
        console.error("Bootstrap database read failed", error?.message || "unknown error");
        return json({ error: "Administrator database check failed (bootstrap-db-read)." }, 500);
      }

      if (existingAdmin) {
        if (existingAdmin.email.toLowerCase() !== email) return json({ error: "That email is not the configured administrator account." }, 403);
        try {
          await env.AUTH_DB.prepare(`UPDATE users SET password_salt=?,password_hash=?,password_iterations=?,is_active=1,auth_version=auth_version+1
            WHERE user_id=? AND role='admin'`).bind(digest.salt, digest.hash, digest.iterations, existingAdmin.user_id).run();
        } catch (error) {
          console.error("Bootstrap database update failed", error?.message || "unknown error");
          return json({ error: "Administrator password save failed (bootstrap-db-update)." }, 500);
        }
        return json({ ok: true, recovered: true });
      }

      try {
        await env.AUTH_DB.prepare(`INSERT INTO users
          (user_id,email,role,password_salt,password_hash,password_iterations,is_active,created_at)
          VALUES (?,?,?,?,?,?,1,?)`)
          .bind(crypto.randomUUID(), email, "admin", digest.salt, digest.hash, digest.iterations, Math.floor(Date.now() / 1000)).run();
      } catch (error) {
        console.error("Bootstrap database insert failed", error?.message || "unknown error");
        return json({ error: "Administrator account creation failed (bootstrap-db-insert)." }, 500);
      }
      return json({ ok: true });
    }

    if (request.method === "POST" && url.pathname === "/login") {
      const rate = await env.AUTH_RATE_LIMITER.limit({ key: "login:" + ip });
      if (!rate.success) return json({ error: "Too many sign-in attempts. Wait a minute and try again." }, 429);
      let body;
      try { body = await request.json(); } catch { return json({ error: "Invalid request." }, 400); }
      const email = validEmail(body?.email);
      const password = String(body?.password || "");
      const user = email ? await env.AUTH_DB.prepare("SELECT * FROM users WHERE email=? LIMIT 1").bind(email).first() : null;
      const dummy = { password_salt: "AAAAAAAAAAAAAAAAAAAAAA", password_hash: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", password_iterations: PASSWORD_ITERATIONS };
      const matches = await passwordMatches(password.slice(0, 128), user?.is_active ? user : dummy);
      if (!user?.is_active || !matches || password.length > 128) return json({ error: "Email or password is incorrect." }, 401);
      const sessionToken = await createSession(env, user);
      return json({ ok: true, role: user.role }, 200, { "set-cookie": sessionCookie(sessionToken) });
    }

    if (request.method === "POST" && url.pathname === "/auth/complete-token") {
      const rate = await env.AUTH_RATE_LIMITER.limit({ key: "complete:" + ip });
      if (!rate.success) return json({ error: "Please wait before trying again." }, 429);
      let body;
      try { body = await request.json(); } catch { return json({ error: "Invalid request." }, 400); }
      const token = String(body?.token || "");
      const password = String(body?.password || "");
      if (token.length < 20 || token.length > 128) return json({ error: "This link is invalid or has expired." }, 400);
      if (!validPassword(password)) return json({ error: "Choose a password between 14 and 128 characters." }, 400);
      const now = Math.floor(Date.now() / 1000);
      const tokenHash = await sha256Hex(token);
      const row = await env.AUTH_DB.prepare(`SELECT t.user_id,t.purpose,u.is_active
        FROM auth_tokens t JOIN users u ON u.user_id=t.user_id
        WHERE t.token_hash=? AND t.used_at IS NULL AND t.expires_at>?`).bind(tokenHash, now).first();
      if (!row || (row.purpose === "invite" && row.is_active) || (row.purpose === "reset" && !row.is_active)) {
        return json({ error: "This link is invalid or has expired." }, 400);
      }
      const digest = await passwordDigest(password);
      const nonce = randomToken(16);
      const results = await env.AUTH_DB.batch([
        env.AUTH_DB.prepare(`UPDATE auth_tokens SET used_at=?,consumed_nonce=?
          WHERE token_hash=? AND used_at IS NULL AND expires_at>? AND purpose=?`)
          .bind(now, nonce, tokenHash, now, row.purpose),
        env.AUTH_DB.prepare(`UPDATE users SET password_salt=?,password_hash=?,password_iterations=?,is_active=1,auth_version=auth_version+1
          WHERE user_id=? AND ((?='invite' AND is_active=0) OR (?='reset' AND is_active=1))
          AND EXISTS (SELECT 1 FROM auth_tokens WHERE token_hash=? AND consumed_nonce=? AND used_at=?)`)
          .bind(digest.salt, digest.hash, digest.iterations, row.user_id, row.purpose, row.purpose, tokenHash, nonce, now),
        env.AUTH_DB.prepare("UPDATE auth_tokens SET consumed_nonce=NULL WHERE token_hash=? AND consumed_nonce=?").bind(tokenHash, nonce),
      ]);
      if (Number(results[0]?.meta?.changes || 0) !== 1 || Number(results[1]?.meta?.changes || 0) !== 1) {
        return json({ error: "This link is invalid or has expired." }, 400);
      }
      return json({ ok: true });
    }

    if (request.method === "POST" && url.pathname === "/logout") {
      const cookie = request.headers.get("cookie") || "";
      const match = cookie.match(/(?:^|;\s*)share_session=([^;]+)/);
      if (match) await env.AUTH_DB.prepare("DELETE FROM sessions WHERE token_hash=?").bind(await sha256Hex(match[1])).run();
      return new Response(null, { status: 204, headers: baseHeaders({ "set-cookie": sessionCookie("", 0) }) });
    }

    const session = await verifySession(request, env);
    if (!session) {
      if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/file/")) return json({ error: "Unauthorized" }, 401);
      return Response.redirect(PORTAL_ORIGIN + "/login", 302);
    }
    if (request.method === "GET" && url.pathname === "/") return html(PORTAL);
    if (request.method === "GET" && url.pathname === "/api/me") return json({ role: session.role, email: session.email });

    if (request.method === "GET" && url.pathname === "/api/users") {
      if (session.role !== "admin") return json({ error: "Admin access required." }, 403);
      const result = await env.AUTH_DB.prepare("SELECT user_id,email,role,is_active,created_at FROM users ORDER BY role,email").all();
      return json({ users: (result.results || []).map(u => ({ user_id: u.user_id, email: u.email, role: u.role, active: !!u.is_active, created_at: u.created_at })) });
    }
    if (request.method === "POST" && url.pathname === "/api/users/invite") {
      if (session.role !== "admin") return json({ error: "Admin access required." }, 403);
      const rate = await env.AUTH_RATE_LIMITER.limit({ key: "admin-invite:" + session.userId });
      if (!rate.success) return json({ error: "Please wait before creating another account." }, 429);
      let body;
      try { body = await request.json(); } catch { return json({ error: "Invalid request." }, 400); }
      const email = validEmail(body?.email);
      if (!email) return json({ error: "Enter a valid email address." }, 400);
      if (await env.AUTH_DB.prepare("SELECT 1 FROM users WHERE email=? LIMIT 1").bind(email).first()) return json({ error: "An account already exists for that email." }, 409);
      const now = Math.floor(Date.now() / 1000);
      const user = { user_id: crypto.randomUUID(), email };
      const raw = randomToken();
      const tokenHash = await sha256Hex(raw);
      try {
        await env.AUTH_DB.batch([
          env.AUTH_DB.prepare("INSERT INTO users (user_id,email,role,is_active,created_at) VALUES (?,?, 'tester',0,?)").bind(user.user_id, email, now),
          env.AUTH_DB.prepare("INSERT INTO auth_tokens (token_hash,user_id,purpose,expires_at,created_at) VALUES (?,?, 'invite',?,?)").bind(tokenHash, user.user_id, now + AUTH_TOKEN_SECONDS, now),
        ]);
      } catch {
        return json({ error: "Could not create this account. Check whether the email is already in use." }, 409);
      }
      return json({ ok: true, email, url: `${PORTAL_ORIGIN}/reset?token=${encodeURIComponent(raw)}` });
    }
    if (request.method === "POST" && url.pathname === "/api/users/reset-link") {
      if (session.role !== "admin") return json({ error: "Admin access required." }, 403);
      const rate = await env.AUTH_RATE_LIMITER.limit({ key: "admin-reset:" + session.userId });
      if (!rate.success) return json({ error: "Please wait before generating another link." }, 429);
      let body;
      try { body = await request.json(); } catch { return json({ error: "Invalid request." }, 400); }
      const user = await env.AUTH_DB.prepare("SELECT user_id,email,role,is_active FROM users WHERE user_id=? LIMIT 1").bind(String(body?.user_id || "")).first();
      if (!user || user.role !== "tester") return json({ error: "Tester account not found." }, 404);
      const purpose = user.is_active ? "reset" : "invite";
      const link = await issueToken(env, user, purpose);
      return json({ ok: true, email: user.email, url: link, purpose });
    }
    if (request.method === "POST" && url.pathname === "/api/users/disable") {
      if (session.role !== "admin") return json({ error: "Admin access required." }, 403);
      let body;
      try { body = await request.json(); } catch { return json({ error: "Invalid request." }, 400); }
      const userId = String(body?.user_id || "");
      const target = await env.AUTH_DB.prepare("SELECT user_id,role FROM users WHERE user_id=? LIMIT 1").bind(userId).first();
      if (!target || target.role !== "tester") return json({ error: "Tester account not found." }, 404);
      await env.AUTH_DB.batch([
        env.AUTH_DB.prepare("UPDATE users SET is_active=0,auth_version=auth_version+1 WHERE user_id=? AND role='tester'").bind(userId),
        env.AUTH_DB.prepare("DELETE FROM auth_tokens WHERE user_id=? AND used_at IS NULL").bind(userId),
      ]);
      return json({ ok: true });
    }

    if (request.method === "GET" && url.pathname === "/api/list") {
      const prefix = normalizeKey(url.searchParams.get("prefix") || "");
      if (!prefix || !FOLDERS.includes(topFolder(prefix))) return json({ error: "Invalid folder." }, 400);
      const listed = await env.SHARE_BUCKET.list({ prefix, include: ["customMetadata"], limit: 1000 });
      return json({ objects: listed.objects.filter(o => !o.key.endsWith("/")).map(o => ({ key: o.key, size: o.size, uploaded: o.uploaded, description: o.customMetadata?.description || "", uploader_role: o.customMetadata?.uploader_role || "" })).sort((a, b) => String(b.uploaded).localeCompare(String(a.uploaded))), truncated: listed.truncated });
    }
    if (request.method === "POST" && url.pathname === "/api/multipart/start") {
      const rate = await env.UPLOAD_RATE_LIMITER.limit({ key: ip });
      if (!rate.success) return json({ error: "Upload rate limit reached." }, 429);
      let body;
      try { body = await request.json(); } catch { return json({ error: "Invalid request." }, 400); }
      const key = normalizeKey(body?.key || "");
      if (!key || !canUpload(session.role, key)) return json({ error: "You cannot upload to that folder." }, 403);
      const size = Number(body?.size || 0);
      if (!Number.isFinite(size) || size <= 0) return json({ error: "File size is required." }, 400);
      if (size > MAX_MULTIPART_BYTES) return json({ error: "File is larger than 10 GB." }, 413);
      const description = String(body?.description || "").slice(0, 500);
      const contentType = String(body?.content_type || "application/octet-stream").slice(0, 200);
      const upload = await env.SHARE_BUCKET.createMultipartUpload(key, {
        httpMetadata: { contentType },
        customMetadata: { description, uploader_role: session.role, uploaded_at: new Date().toISOString() },
      });
      return json({ ok: true, key, upload_id: upload.uploadId, part_size: MULTIPART_PART_BYTES });
    }
    if (request.method === "POST" && url.pathname === "/api/multipart/part") {
      const rate = await env.UPLOAD_RATE_LIMITER.limit({ key: ip });
      if (!rate.success) return json({ error: "Upload rate limit reached." }, 429);
      const key = normalizeKey(url.searchParams.get("key") || "");
      const uploadId = String(url.searchParams.get("upload_id") || "");
      const partNumber = Number(url.searchParams.get("part") || 0);
      if (!key || !canUpload(session.role, key)) return json({ error: "You cannot upload to that folder." }, 403);
      if (!uploadId || !Number.isInteger(partNumber) || partNumber < 1 || partNumber > 10000) return json({ error: "Invalid multipart upload." }, 400);
      const rawLength = Number(request.headers.get("content-length") || "0");
      if (rawLength > MULTIPART_PART_BYTES) return json({ error: "Upload part is too large." }, 413);
      if (!request.body) return json({ error: "Empty upload part." }, 400);
      const upload = env.SHARE_BUCKET.resumeMultipartUpload(key, uploadId);
      const part = await upload.uploadPart(partNumber, request.body);
      return json({ ok: true, partNumber: part.partNumber, etag: part.etag });
    }
    if (request.method === "POST" && url.pathname === "/api/multipart/complete") {
      let body;
      try { body = await request.json(); } catch { return json({ error: "Invalid request." }, 400); }
      const key = normalizeKey(body?.key || "");
      const uploadId = String(body?.upload_id || "");
      const parts = Array.isArray(body?.parts) ? body.parts.map(p => ({ partNumber: Number(p.partNumber), etag: String(p.etag || "") })) : [];
      if (!key || !canUpload(session.role, key)) return json({ error: "You cannot upload to that folder." }, 403);
      if (!uploadId || !parts.length || parts.length > 10000 || parts.some(p => !Number.isInteger(p.partNumber) || p.partNumber < 1 || !p.etag)) return json({ error: "Invalid multipart completion." }, 400);
      parts.sort((a,b)=>a.partNumber-b.partNumber);
      if (parts.some((p,i)=>i>0 && p.partNumber===parts[i-1].partNumber)) return json({ error: "Duplicate upload part." }, 400);
      const upload = env.SHARE_BUCKET.resumeMultipartUpload(key, uploadId);
      const object = await upload.complete(parts);
      return json({ ok: true, key, size: object.size || null });
    }
    if (request.method === "POST" && url.pathname === "/api/multipart/abort") {
      let body;
      try { body = await request.json(); } catch { return json({ error: "Invalid request." }, 400); }
      const key = normalizeKey(body?.key || "");
      const uploadId = String(body?.upload_id || "");
      if (!key || !canUpload(session.role, key) || !uploadId) return json({ error: "Invalid multipart upload." }, 400);
      await env.SHARE_BUCKET.resumeMultipartUpload(key, uploadId).abort();
      return json({ ok: true });
    }
    if (request.method === "POST" && url.pathname === "/api/upload") {
      const rate = await env.UPLOAD_RATE_LIMITER.limit({ key: ip });
      if (!rate.success) return json({ error: "Upload rate limit reached." }, 429);
      const rawLength = Number(request.headers.get("content-length") || "0");
      if (rawLength > MAX_UPLOAD_BYTES) return json({ error: "File is larger than 75 MB." }, 413);
      const key = normalizeKey(request.headers.get("x-file-path") || "");
      if (!key || !canUpload(session.role, key)) return json({ error: "You cannot upload to that folder." }, 403);
      const body = await request.arrayBuffer();
      if (!body.byteLength) return json({ error: "Empty upload." }, 400);
      if (body.byteLength > MAX_UPLOAD_BYTES) return json({ error: "File is larger than 75 MB." }, 413);
      let description;
      try { description = decodeURIComponent(request.headers.get("x-description") || "").slice(0, 500); } catch { description = ""; }
      await env.SHARE_BUCKET.put(key, body, { httpMetadata: { contentType: request.headers.get("content-type") || "application/octet-stream" }, customMetadata: { description, uploader_role: session.role, uploaded_at: new Date().toISOString() } });
      return json({ ok: true, key, size: body.byteLength });
    }
    if (request.method === "GET" && url.pathname.startsWith("/file/")) {
      const key = normalizeKey(url.pathname.slice("/file/".length));
      if (!key || !FOLDERS.includes(topFolder(key))) return json({ error: "Invalid file." }, 400);
      const obj = await env.SHARE_BUCKET.get(key);
      if (!obj) return json({ error: "File not found." }, 404);
      return new Response(obj.body, { headers: baseHeaders({ "content-type": obj.httpMetadata?.contentType || "application/octet-stream", "content-disposition": `attachment; filename="${safeFilename(key.split("/").pop())}"` }) });
    }
    if (request.method === "DELETE" && url.pathname === "/api/file") {
      if (session.role !== "admin") return json({ error: "Admin access required." }, 403);
      const key = normalizeKey(url.searchParams.get("key") || "");
      if (!key || !FOLDERS.includes(topFolder(key))) return json({ error: "Invalid file." }, 400);
      await env.SHARE_BUCKET.delete(key);
      const latest = await readLatest(env);
      if (latest?.key === key) await env.SHARE_BUCKET.delete(META_LATEST);
      return json({ ok: true });
    }
    if (request.method === "GET" && url.pathname === "/api/latest") return json({ latest: await readLatest(env) });
    if (request.method === "POST" && url.pathname === "/api/latest") {
      if (session.role !== "admin") return json({ error: "Admin access required." }, 403);
      let body;
      try { body = await request.json(); } catch { return json({ error: "Invalid request." }, 400); }
      const key = normalizeKey(body?.key || "");
      if (!key || topFolder(key) !== "Releases") return json({ error: "Latest build must be in Releases." }, 400);
      const existing = await env.SHARE_BUCKET.head(key);
      if (!existing) return json({ error: "Release not found." }, 404);
      const latest = { key, marked_at: new Date().toISOString() };
      await env.SHARE_BUCKET.put(META_LATEST, JSON.stringify(latest), { httpMetadata: { contentType: "application/json" } });
      return json({ ok: true, latest });
    }
    return json({ error: "Not found." }, 404);
  } catch (error) {
    console.error("Share portal request failed", error?.message || "unknown error");
    return json({ error: "The portal could not complete that request. Try again or contact the administrator." }, 500);
  }
}};
