// plainkeep share worker — a capability-URL pastebin for plaintext "OPSX" bundles. ~120 lines, no deps.
//
// Model: the client PUTs a plaintext OPSX bundle (markdown + rendered HTML). The worker stores it in
// KV under a long unguessable id and serves either half on GET. There is NO encryption: the id IS the
// secret (~124 bits, randId(24)). Secrecy = link secrecy + TTL. Whoever holds the URL can read it;
// hold it privately and let it expire. Legacy AES-GCM ciphertext shares are dead — served as 410.
//
// OPSX v1 blob: "OPSX" magic + version(1) + 3 zero bytes + uint32 mdLen(BE) + uint32 htmlLen(BE) +
//   md bytes + html bytes. The HTML half is fully self-contained (inline CSS, data-URI images, no JS).
//
// Routes:
//   PUT  /             body = OPSX blob; X-Expire-Seconds = TTL (default 604800, min 60);
//                      X-Publish-Token must match env.PUBLISH_TOKEN when that secret is set
//                      -> { id, admin_token }
//   GET  /<id>         Accept: text/html (browser) -> the HTML half; else -> the markdown half
//   GET  /<id>.md      -> the markdown half (text/markdown)
//   GET  /<id>?raw=1   -> the whole stored OPSX blob (application/octet-stream)
//   DELETE /<id>       X-Admin-Token -> 204 (admin only)
//   (legacy non-OPSX blob on any GET -> 410 gone)

const ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789";
const OPSX_MAGIC = [0x4f, 0x50, 0x53, 0x58]; // "OPSX"

function unpackBundle(u8) {
  if (!u8 || u8.length < 16) return null;
  const magic = String.fromCharCode(u8[0], u8[1], u8[2], u8[3]);
  if (magic !== "OPSX" || u8[4] !== 1) return null;
  const mdLen = (u8[8] << 24) | (u8[9] << 16) | (u8[10] << 8) | u8[11];
  const htmlLen = (u8[12] << 24) | (u8[13] << 16) | (u8[14] << 8) | u8[15];
  const off = 16;
  if (off + mdLen + htmlLen > u8.length) return null;
  return {
    md: u8.slice(off, off + mdLen),
    html: u8.slice(off + mdLen, off + mdLen + htmlLen),
  };
}

function isOpsx(u8) {
  return (
    u8.length >= 4 &&
    u8[0] === OPSX_MAGIC[0] &&
    u8[1] === OPSX_MAGIC[1] &&
    u8[2] === OPSX_MAGIC[2] &&
    u8[3] === OPSX_MAGIC[3]
  );
}

function randId(n = 24) {
  const buf = new Uint8Array(n);
  crypto.getRandomValues(buf);
  return Array.from(buf, (b) => ID_ALPHABET[b % ID_ALPHABET.length]).join("");
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// A stored blob that is not an OPSX bundle is a legacy encrypted/HTML-only share — permanently dead.
const GONE = () =>
  json({ error: "legacy encrypted share — re-publish with current plainkeep share" }, 410);

function halfResponse(body, contentType, extraHeaders) {
  return new Response(body, {
    headers: {
      "Content-Type": contentType,
      "Cache-Control": "no-store",
      "X-Robots-Tag": "noindex",
      ...extraHeaders,
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/^\/+/, "");

    // PUT / — publish an OPSX blob.
    if (request.method === "PUT" && path === "") {
      // FAIL CLOSED. This used to read `if (env.PUBLISH_TOKEN && …)`, so a Worker with no secret
      // accepted every publish: the `&&` short-circuited and the comparison never ran. A freshly
      // deployed Worker has no secrets, which made "deployed but not yet configured" an open,
      // unauthenticated write endpoint. Absence of the secret is now a refusal, not a bypass.
      if (!env.PUBLISH_TOKEN) {
        return json({ error: "worker has no PUBLISH_TOKEN", hint: "run: plainkeep share init --yes" }, 503);
      }
      if (request.headers.get("X-Publish-Token") !== env.PUBLISH_TOKEN) {
        return json({ error: "publish token required", hint: "run: plainkeep share init" }, 401);
      }
      const ttl = Math.max(60, parseInt(request.headers.get("X-Expire-Seconds") || "604800", 10));
      const body = await request.arrayBuffer();
      if (body.byteLength === 0) return json({ error: "empty body" }, 400);
      if (body.byteLength > 24 * 1024 * 1024) return json({ error: "too large" }, 413);
      const id = randId(24); // the URL is now the entire secret (~124 bits)
      const admin = randId(24);
      await env.PLAINKEEP_SHARE.put("blob:" + id, body, { expirationTtl: ttl });
      await env.PLAINKEEP_SHARE.put("admin:" + id, admin, { expirationTtl: ttl });
      return json({ id, admin_token: admin });
    }

    // Match /<id> or /<id>.md — 10..32 chars so legacy 10-char ids still route.
    const idMatch = path.match(/^([a-z0-9]{10,32})(\.md)?$/i);
    const id = idMatch ? idMatch[1] : null;
    const dotMd = idMatch ? Boolean(idMatch[2]) : false;

    if (request.method === "GET" && id) {
      const blob = await env.PLAINKEEP_SHARE.get("blob:" + id, "arrayBuffer");
      if (!blob) return json({ error: "not found" }, 404);
      const bytes = new Uint8Array(blob);
      if (!isOpsx(bytes)) return GONE(); // legacy blobs are dead on every GET

      // ?raw=1 — the whole OPSX blob (plainkeep share pull, debugging).
      if (url.searchParams.has("raw")) {
        return halfResponse(bytes, "application/octet-stream");
      }

      const unpacked = unpackBundle(bytes);
      if (!unpacked) return GONE(); // malformed OPSX — treat as dead

      const wantsHtml = (request.headers.get("Accept") || "").includes("text/html");
      // /<id> in a browser -> HTML half; /<id>.md or non-browser -> markdown half.
      if (!dotMd && wantsHtml) {
        return halfResponse(unpacked.html, "text/html; charset=utf-8", {
          "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'",
          Link: `</${id}.md>; rel="alternate"; type="text/markdown"`,
        });
      }
      return halfResponse(unpacked.md, "text/markdown; charset=utf-8");
    }

    if (request.method === "DELETE" && id) {
      const admin = await env.PLAINKEEP_SHARE.get("admin:" + id);
      if (!admin) return json({ error: "not found" }, 404);
      if (request.headers.get("X-Admin-Token") !== admin) return json({ error: "forbidden" }, 403);
      await env.PLAINKEEP_SHARE.delete("blob:" + id);
      await env.PLAINKEEP_SHARE.delete("admin:" + id);
      return new Response(null, { status: 204 });
    }

    return json({ error: "method not allowed" }, 405);
  },
};
