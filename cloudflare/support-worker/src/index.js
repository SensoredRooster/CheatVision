const MAX_BYTES = 75 * 1024 * 1024;

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function safeName(value, fallback = "unknown") {
  const clean = String(value || "").trim().replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[.-]+|[.-]+$/g, "");
  return clean.slice(0, 140) || fallback;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/health") {
      return json({ ok: true, service: "cheatvision-support-collector" });
    }

    if (request.method === "POST" && url.pathname === "/upload") {
      const ip = request.headers.get("cf-connecting-ip") || "unknown";
      const rate = await env.UPLOAD_RATE_LIMITER.limit({ key: ip });
      if (!rate.success) return json({ error: "Too many uploads. Try again later." }, 429);

      const length = Number(request.headers.get("content-length") || "0");
      if (length > MAX_BYTES) return json({ error: "Bundle too large" }, 413);

      const body = await request.arrayBuffer();
      if (!body.byteLength) return json({ error: "Empty bundle" }, 400);
      if (body.byteLength > MAX_BYTES) return json({ error: "Bundle too large" }, 413);

      const bytes = new Uint8Array(body);
      if (bytes.length < 2 || bytes[0] !== 0x50 || bytes[1] !== 0x4b) {
        return json({ error: "Expected a ZIP support bundle" }, 415);
      }

      const session = safeName(request.headers.get("x-cheatvision-session"), "unknown-session");
      const original = safeName(request.headers.get("x-cheatvision-filename"), "CheatVision-Support.zip");
      const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
      const key = `bundles/${stamp}-${session}-${original}`;

      await env.SUPPORT_BUCKET.put(key, body, {
        httpMetadata: { contentType: "application/zip" },
        customMetadata: { session, receivedAt: new Date().toISOString() },
      });

      return json({ ok: true, bundle: key.split("/").pop(), session_id: session });
    }

    return json({ error: "Not found" }, 404);
  },
};
