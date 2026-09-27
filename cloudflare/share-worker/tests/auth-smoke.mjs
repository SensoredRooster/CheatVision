import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createServer } from "node:net";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const workerDir = fileURLToPath(new URL("..", import.meta.url));
const stateDir = await mkdtemp(join(tmpdir(), "cheatvision-share-smoke-"));
const secret = "smoke-test-only-bootstrap-key";
const isWindows = process.platform === "win32";
const shell = isWindows;
let child;
let output = "";

function runWrangler(args) {
  const result = spawnSync("wrangler", args, { cwd: workerDir, encoding: "utf8", shell });
  if (result.status !== 0) throw new Error(`Wrangler command failed: ${(result.stderr || result.stdout || "").slice(-2500)}`);
}

async function freePort() {
  const server = createServer();
  await new Promise((resolve, reject) => server.once("error", reject).listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  await new Promise(resolve => server.close(resolve));
  return port;
}

async function request(base, path, { method = "GET", body, cookie, extraHeaders = {}, rawBody = false } = {}) {
  const headers = { ...extraHeaders };
  if (body !== undefined && !rawBody) headers["content-type"] = "application/json";
  if (cookie) headers.cookie = cookie;
  const response = await fetch(base + path, {
    method,
    headers,
    body: body === undefined ? undefined : rawBody ? body : JSON.stringify(body),
    redirect: "manual",
  });
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { /* HTML page */ }
  return { response, data };
}

function cookieFrom(response) {
  return (response.headers.get("set-cookie") || "").split(";")[0];
}

try {
  runWrangler(["d1", "migrations", "apply", "cheatvision-share-auth", "--local", "--persist-to", stateDir]);
  const port = await freePort();
  child = spawn("wrangler", [
    "dev", "--local", "--persist-to", stateDir,
    "--var", `BOOTSTRAP_SECRET:${secret}`, "--port", String(port),
  ], { cwd: workerDir, shell, stdio: ["ignore", "pipe", "pipe"] });
  child.stdout.on("data", chunk => { output = (output + chunk.toString()).slice(-4000); });
  child.stderr.on("data", chunk => { output = (output + chunk.toString()).slice(-4000); });

  const base = `http://127.0.0.1:${port}`;
  let ready = false;
  for (let attempt = 0; attempt < 50; attempt++) {
    if (child.exitCode !== null) throw new Error(`Local Worker exited early. ${output}`);
    try {
      const health = await request(base, "/health");
      if (health.response.ok && health.data.auth === "d1") { ready = true; break; }
    } catch { /* Worker is still starting */ }
    await new Promise(resolve => setTimeout(resolve, 300));
  }
  assert.ok(ready, `Local Worker did not become ready. ${output}`);

  const bootstrap = await request(base, "/auth/bootstrap", {
    method: "POST",
    body: { setup_key: secret, email: "admin@example.test", password: "local-only-admin-password-123" },
  });
  assert.equal(bootstrap.response.status, 200, "initial admin setup");

  let adminLogin = await request(base, "/login", {
    method: "POST",
    body: { email: "admin@example.test", password: "local-only-admin-password-123" },
  });
  if (adminLogin.response.status !== 200) await new Promise(resolve => setTimeout(resolve, 150));
  let adminCookie = cookieFrom(adminLogin.response);
  assert.equal(adminLogin.data.role, "admin", `admin login returned HTTP ${adminLogin.response.status}: ${JSON.stringify(adminLogin.data)} ${output}`);
  assert.ok(adminCookie.startsWith("share_session="), "opaque session cookie is issued");
  assert.equal((await request(base, "/api/me", { cookie: adminCookie })).data.email, "admin@example.test");

  const invitation = await request(base, "/api/users/invite", {
    method: "POST", cookie: adminCookie, body: { email: "tester@example.test" },
  });
  assert.equal(invitation.response.status, 200, "admin can create tester and receive a setup link");
  const setupToken = new URL(invitation.data.url).searchParams.get("token");
  const setup = await request(base, "/auth/complete-token", {
    method: "POST", body: { token: setupToken, password: "local-only-tester-password-123" },
  });
  assert.equal(setup.response.status, 200, "tester can set an individual password");
  const replay = await request(base, "/auth/complete-token", {
    method: "POST", body: { token: setupToken, password: "local-only-tester-password-456" },
  });
  assert.equal(replay.response.status, 400, "setup link is single-use");

  const testerLogin = await request(base, "/login", {
    method: "POST", body: { email: "tester@example.test", password: "local-only-tester-password-123" },
  });
  const testerCookie = cookieFrom(testerLogin.response);
  assert.equal(testerLogin.data.role, "tester", "tester login");
  assert.equal((await request(base, "/api/users", { cookie: testerCookie })).response.status, 403, "tester cannot administer accounts");
  assert.equal((await request(base, "/api/upload", {
    method: "POST", cookie: testerCookie,
    body: "blocked", rawBody: true, extraHeaders: { "x-file-path": encodeURIComponent("Releases/blocked.txt") },
  })).response.status, 403, "tester cannot upload to Releases");
  assert.equal((await request(base, "/api/upload", {
    method: "POST", cookie: testerCookie, body: "sample", rawBody: true,
    extraHeaders: { "x-file-path": encodeURIComponent("Tester Uploads/sample.txt") },
  })).response.status, 200, "tester can upload to an allowed folder");

  const multipartStart = await request(base, "/api/multipart/start", {
    method: "POST", cookie: testerCookie,
    body: { key: "VODs/Incoming/test-vod.mp4", description: "multipart smoke", content_type: "video/mp4", size: 6 },
  });
  assert.equal(multipartStart.response.status, 200, "tester can start a VOD multipart upload");
  const multipartPart = await request(base, "/api/multipart/part?key="+encodeURIComponent("VODs/Incoming/test-vod.mp4")+"&upload_id="+encodeURIComponent(multipartStart.data.upload_id)+"&part=1", {
    method: "POST", cookie: testerCookie, body: "abcdef", rawBody: true,
    extraHeaders: { "content-type": "application/octet-stream" },
  });
  assert.equal(multipartPart.response.status, 200, "tester can upload a multipart VOD part");
  const multipartComplete = await request(base, "/api/multipart/complete", {
    method: "POST", cookie: testerCookie,
    body: { key: "VODs/Incoming/test-vod.mp4", upload_id: multipartStart.data.upload_id, parts: [{ partNumber: multipartPart.data.partNumber, etag: multipartPart.data.etag }] },
  });
  assert.equal(multipartComplete.response.status, 200, "tester can complete a VOD multipart upload");
  const vodList = await request(base, "/api/list?prefix="+encodeURIComponent("VODs/Incoming/"), { cookie: testerCookie });
  assert.ok(vodList.data.objects.some(o => o.key === "VODs/Incoming/test-vod.mp4"), "completed VOD appears in Incoming");
  assert.equal((await request(base, "/api/multipart/start", {
    method: "POST", cookie: testerCookie,
    body: { key: "VODs/Reviewed/blocked.mp4", content_type: "video/mp4", size: 6 },
  })).response.status, 403, "tester cannot upload directly to VODs/Reviewed");

  const roster = await request(base, "/api/users", { cookie: adminCookie });
  const tester = roster.data.users.find(user => user.role === "tester");
  assert.ok(tester, "admin sees tester roster");
  const reset = await request(base, "/api/users/reset-link", {
    method: "POST", cookie: adminCookie, body: { user_id: tester.user_id },
  });
  const resetToken = new URL(reset.data.url).searchParams.get("token");
  assert.equal((await request(base, "/auth/complete-token", {
    method: "POST", body: { token: resetToken, password: "local-only-tester-password-789" },
  })).response.status, 200, "admin reset link changes tester password");
  assert.equal((await request(base, "/api/me", { cookie: testerCookie })).response.status, 401, "reset invalidates existing tester sessions");

  const newTesterLogin = await request(base, "/login", {
    method: "POST", body: { email: "tester@example.test", password: "local-only-tester-password-789" },
  });
  assert.equal(newTesterLogin.data.role, "tester", "tester can sign in with reset password");
  const disabled = await request(base, "/api/users/disable", {
    method: "POST", cookie: adminCookie, body: { user_id: tester.user_id },
  });
  assert.equal(disabled.response.status, 200, "admin can disable tester");
  assert.equal((await request(base, "/api/me", { cookie: cookieFrom(newTesterLogin.response) })).response.status, 401, "disable revokes existing tester sessions");

  const recovered = await request(base, "/auth/bootstrap", {
    method: "POST",
    body: { setup_key: secret, email: "admin@example.test", password: "local-only-admin-password-456" },
  });
  assert.equal(recovered.response.status, 200, "admin can recover with private setup key");
  assert.equal((await request(base, "/api/me", { cookie: adminCookie })).response.status, 401, "admin recovery invalidates old sessions");
  adminLogin = await request(base, "/login", {
    method: "POST", body: { email: "admin@example.test", password: "local-only-admin-password-456" },
  });
  assert.equal(adminLogin.data.role, "admin", "recovered admin can sign in");

  process.stdout.write("Share portal auth smoke tests passed (setup, login, invite, single-use reset, role checks, deactivation, recovery).\n");
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
} finally {
  if (child) {
    const closed = new Promise(resolve => child.once("close", resolve));
    if (isWindows && child.pid) {
      spawnSync("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], { encoding: "utf8" });
    } else if (child.exitCode === null) {
      child.kill("SIGTERM");
    }
    const stopped = await Promise.race([closed.then(() => true), new Promise(resolve => setTimeout(() => resolve(false), 5000))]);
    if (!stopped) {
      if (isWindows && child.pid) spawnSync("taskkill.exe", ["/PID", String(child.pid), "/T", "/F"], { encoding: "utf8" });
      else child.kill("SIGKILL");
      await Promise.race([closed, new Promise(resolve => setTimeout(resolve, 5000))]);
    }
  }
  for (let attempt = 0; attempt < 10; attempt++) {
    try {
      await rm(stateDir, { recursive: true, force: true });
      break;
    } catch (error) {
      if (error?.code !== "EBUSY" || attempt === 9) throw error;
      await new Promise(resolve => setTimeout(resolve, 300));
    }
  }
}
