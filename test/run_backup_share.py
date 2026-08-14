#!/usr/bin/env python3
"""
run_backup_share.py — offline suite for the backup family (Part 5.1) + `plainkeep share` (Part 5.2) +
sweep share hygiene. NEVER contacts a network endpoint: the share transport is short-circuited by
PLAINKEEP_SHARE_FAKE, and restic paths run only their graceful-degrade branches (restic is not required).

`plainkeep share` is a plaintext capability-URL model now — no crypto anywhere. The JS half of the worker
is covered by a Node route-contract test (below), which replaces the old Web-Crypto known-answer test.
"""
from __future__ import annotations
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def _load_sharelib():
    """Load bin/lib/sharelib.py by file path (bin/lib namespace loses to test/lib on sys.path)."""
    spec = importlib.util.spec_from_file_location("sharelib", REPO / "bin" / "lib" / "sharelib.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(home, *a):
    return subprocess.run(["git", "-C", str(home), *a], capture_output=True, text=True)


def init_git(home):
    git(home, "init", "-q", "-b", "main")
    git(home, "config", "user.email", "t@e"); git(home, "config", "user.name", "t")
    git(home, "config", "commit.gpgsign", "false")


def run(verb, *args, home, roots=None, extra_env=None):
    env = {**os.environ, "PLAINKEEP_HOME": str(home)}
    if roots:
        env["PLAINKEEP_ROOTS_HOME"] = str(roots)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, str(REPO / "bin" / verb / "run.py"), *args],
                          capture_output=True, text=True, env=env)


# The JS route-contract test. Drives bin/share/worker/worker.js through its real routes with a
# Map-backed KV stub. `__WORKER_URL__` is replaced with the worker.js file:// URL before running.
_WORKER_MJS = r"""
import worker from '__WORKER_URL__';

function kvStub() {
  const m = new Map();
  return {
    _map: m,
    async get(key, type) {
      const v = m.get(key);
      if (v === undefined || v === null) return null;
      if (type === "arrayBuffer") {
        if (v instanceof ArrayBuffer) return v;
        if (v instanceof Uint8Array) return v.buffer.slice(v.byteOffset, v.byteOffset + v.byteLength);
        return v;
      }
      return v;
    },
    async put(key, value) { m.set(key, value); },
    async delete(key) { m.delete(key); },
  };
}

function buildOpsx(md, html) {
  const enc = new TextEncoder();
  const mdB = enc.encode(md), htmlB = enc.encode(html);
  const out = new Uint8Array(16 + mdB.length + htmlB.length);
  out[0] = 0x4f; out[1] = 0x50; out[2] = 0x53; out[3] = 0x58; out[4] = 1; // "OPSX" + version 1
  const dv = new DataView(out.buffer);
  dv.setUint32(8, mdB.length, false);   // BE uint32
  dv.setUint32(12, htmlB.length, false);
  out.set(mdB, 16);
  out.set(htmlB, 16 + mdB.length);
  return out;
}

const fails = [];
const A = (cond, msg) => { if (!cond) fails.push(msg); };

(async () => {
  // A CONFIGURED worker: it has its PUBLISH_TOKEN secret. This used to be `undefined` here, back
  // when a missing secret meant "no auth required" — which is exactly the hole that made a freshly
  // deployed worker an open write endpoint. The unconfigured case is asserted separately below.
  const env = { PLAINKEEP_SHARE: kvStub(), PUBLISH_TOKEN: "sekret" };
  const AUTH = { "X-Publish-Token": "sekret" };
  const blob = buildOpsx("# hello agent", "<!doctype html><p>hi</p>");

  // PUT / → 200 JSON with a 24-char id + admin_token
  let r = await worker.fetch(new Request("https://w/", { method: "PUT", body: blob, headers: AUTH }), env);
  A(r.status === 200, "PUT status " + r.status);
  const j = await r.json();
  A(typeof j.id === "string" && /^[a-z0-9]{24}$/.test(j.id), "PUT id " + j.id);
  A(typeof j.admin_token === "string" && j.admin_token.length > 0, "PUT missing admin_token");
  const id = j.id, admin = j.admin_token;

  // PUBLISH_TOKEN gating: missing header → 401; matching header → 200
  const envT = { PLAINKEEP_SHARE: kvStub(), PUBLISH_TOKEN: "sekret" };
  r = await worker.fetch(new Request("https://w/", { method: "PUT", body: blob }), envT);
  A(r.status === 401, "PUT no-token status " + r.status);
  r = await worker.fetch(new Request("https://w/", { method: "PUT", body: blob, headers: { "X-Publish-Token": "sekret" } }), envT);
  A(r.status === 200, "PUT good-token status " + r.status);

  // FAIL CLOSED on an UNCONFIGURED worker. A freshly deployed worker has no secrets; treating that
  // as "no auth required" published an open, unauthenticated write endpoint on the user's account.
  // Absence of the secret must be a refusal, and no header may talk its way past it.
  const envNone = { PLAINKEEP_SHARE: kvStub(), PUBLISH_TOKEN: undefined };
  r = await worker.fetch(new Request("https://w/", { method: "PUT", body: blob }), envNone);
  A(r.status === 503, "PUT unconfigured worker status " + r.status);
  r = await worker.fetch(new Request("https://w/", { method: "PUT", body: blob, headers: { "X-Publish-Token": "guess" } }), envNone);
  A(r.status === 503, "PUT unconfigured worker w/ header status " + r.status);

  // GET /<id> Accept: text/html → the html half, with CSP + Link headers
  r = await worker.fetch(new Request("https://w/" + id, { headers: { Accept: "text/html" } }), env);
  A(r.status === 200, "GET html status " + r.status);
  A((r.headers.get("Content-Type") || "").startsWith("text/html"), "GET html ct " + r.headers.get("Content-Type"));
  const htmlBody = await r.text();
  A(htmlBody.includes("<p>hi</p>"), "GET html body");
  A(!!r.headers.get("Content-Security-Policy"), "GET html missing CSP");
  A(!!r.headers.get("Link"), "GET html missing Link");

  // GET /<id> Accept: */* → the markdown half
  r = await worker.fetch(new Request("https://w/" + id, { headers: { Accept: "*/*" } }), env);
  A((r.headers.get("Content-Type") || "").startsWith("text/markdown"), "GET md ct " + r.headers.get("Content-Type"));
  A((await r.text()) === "# hello agent", "GET md body");

  // GET /<id>.md → markdown half even when Accept prefers html
  r = await worker.fetch(new Request("https://w/" + id + ".md", { headers: { Accept: "text/html" } }), env);
  A((r.headers.get("Content-Type") || "").startsWith("text/markdown"), "GET .md ct " + r.headers.get("Content-Type"));
  A((await r.text()) === "# hello agent", "GET .md body");

  // GET /<id>?raw=1 → octet-stream, exact stored bytes
  r = await worker.fetch(new Request("https://w/" + id + "?raw=1"), env);
  A((r.headers.get("Content-Type") || "").startsWith("application/octet-stream"), "raw ct " + r.headers.get("Content-Type"));
  const raw = new Uint8Array(await r.arrayBuffer());
  A(raw.length === blob.length && raw.every((b, i) => b === blob[i]), "raw bytes mismatch");

  // non-OPSX stored blob → 410 on GET
  const id2 = "b".repeat(24);
  env.PLAINKEEP_SHARE._map.set("blob:" + id2, new Uint8Array([1, 2, 3, 4, 5]));
  r = await worker.fetch(new Request("https://w/" + id2 + ".md"), env);
  A(r.status === 410, "non-OPSX status " + r.status);

  // unknown id → 404
  r = await worker.fetch(new Request("https://w/nonexistent24charidxxxxxx"), env);
  A(r.status === 404, "404 status " + r.status);

  // DELETE wrong token → 403, right token → 204, then GET → 404
  r = await worker.fetch(new Request("https://w/" + id, { method: "DELETE", headers: { "X-Admin-Token": "wrong" } }), env);
  A(r.status === 403, "DELETE wrong-token status " + r.status);
  r = await worker.fetch(new Request("https://w/" + id, { method: "DELETE", headers: { "X-Admin-Token": admin } }), env);
  A(r.status === 204, "DELETE right-token status " + r.status);
  r = await worker.fetch(new Request("https://w/" + id, { headers: { Accept: "*/*" } }), env);
  A(r.status === 404, "GET after DELETE status " + r.status);

  if (fails.length) { process.stderr.write(fails.join("\n") + "\n"); process.exit(1); }
  process.stdout.write("WORKER-OK");
})().catch((e) => { process.stderr.write(String((e && e.stack) || e)); process.exit(1); });
"""


def case_share_init_config_is_vault_owned() -> None:
    """`share init`'s per-vault config must land in the VAULT, not beside its own source.

    `bin/share/worker/` is engine-owned and an installed engine is sealed 0555, so anchoring
    `wrangler.toml` at `Path(__file__).parent / "worker"` meant the only way to configure the share
    worker was a write into a read-only tree — which the path-wall correctly refused, leaving the
    verb dead and its printed instructions telling an operator to edit files they cannot edit.

    Driven through a REAL sealed engine, because the checkout it was developed against is writable
    and shows none of this."""
    with tempfile.TemporaryDirectory(prefix="pk-share-sealed-") as td:
        tmp = Path(os.path.realpath(td))
        inst, vault = tmp / "install", tmp / "vault"
        (vault / "wiki").mkdir(parents=True)
        env = {**os.environ, "PLAINKEEP_ENGINE_HOME": str(inst)}
        env.pop("PLAINKEEP_ENGINE", None)
        r = subprocess.run([sys.executable, str(REPO / "bin" / "lib" / "enginetree.py"),
                            "--install", str(REPO)], capture_output=True, text=True, env=env)
        eng = Path(os.path.realpath(inst / "engine" / "current"))
        check("fixture: a sealed engine, with the worker dir read-only",
              r.returncode == 0 and not os.access(eng / "bin" / "share" / "worker", os.W_OK),
              r.stdout + r.stderr)

        venv = {**os.environ, "PLAINKEEP_HOME": str(vault)}
        venv.pop("PLAINKEEP_TEST_HOME", None)
        # In-process, because `share init --yes` (the only caller) is confirm-gated behind a real
        # `wrangler deploy` and can never run in a suite. The function under test is the one that
        # decides WHERE the config goes.
        probe = ("import runpy, sys\n"
                 "g = runpy.run_path(sys.argv[1])\n"
                 "g['_ensure_wrangler_toml']()\n"
                 "print(g['WRANGLER_TOML'])\n")
        # `cwd` away from test/: `-c` puts the CWD on sys.path, and this suite's own `test/lib/`
        # would otherwise shadow the engine's `bin/lib/`.
        p = subprocess.run([sys.executable, "-c", probe, str(eng / "bin" / "share" / "run.py")],
                           capture_output=True, text=True, env=venv, cwd=str(tmp))
        out = (p.stdout + p.stderr).strip()
        toml = vault / ".share" / "wrangler.toml"
        check("_ensure_wrangler_toml() succeeds against a SEALED engine",
              p.returncode == 0 and "DENY" not in out, out[-400:])
        check("...and the config landed in the VAULT, not in the engine",
              toml.is_file() and str(eng) not in p.stdout, f"{p.stdout.strip()} exists={toml.is_file()}")
        check("...and nothing was written into the engine's worker dir",
              not (eng / "bin" / "share" / "worker" / "wrangler.toml").exists())

        # ...and the human instructions name the same path. A verb that writes to the right place
        # while telling the operator to edit the wrong one is the same bug, one layer up.
        r = subprocess.run([sys.executable, str(eng / "bin" / "share" / "run.py"), "init"],
                           capture_output=True, text=True, env=venv)
        steps = r.stdout + r.stderr
        check("`share init` prints steps that name the vault-side config",
              str(toml) in steps, steps[-400:])
        check("...and never tells the operator to write inside the engine tree",
              f"cp wrangler.toml.example wrangler.toml" not in steps
              and f"cd {eng / 'bin' / 'share' / 'worker'}\n" not in steps, steps[-400:])

        for p2 in tmp.rglob("*"):                # the sealed tree cannot be removed as-is
            try:
                if p2.is_dir() and not p2.is_symlink():
                    p2.chmod(0o755)
            except OSError:
                pass


def main() -> int:
    sl = _load_sharelib()
    case_share_init_config_is_vault_owned()

    # ---------- worker route contract (Node): the JS half of the capability-URL model ----------
    # Replaces the old Web-Crypto KAT as the JS-side coverage. Needs only `node`; skips gracefully
    # (informational PASS) when node is absent, mirroring how the old KAT block handled node's absence.
    import shutil as _sh
    worker_url = (REPO / "bin" / "share" / "worker" / "worker.js").as_uri()
    if _sh.which("node"):
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
            f.write(_WORKER_MJS.replace("__WORKER_URL__", worker_url)); mjs = f.name
        p = subprocess.run(["node", mjs], capture_output=True, text=True)
        os.unlink(mjs)
        check("worker routes: PUT/GET(html·md·.md·raw)/410/404/DELETE all pass (Node)",
              "WORKER-OK" in p.stdout, (p.stdout + p.stderr).strip()[-300:])
    else:
        check("worker routes: node absent — JS route cross-check skipped (informational)", True)

    # ---------- sharelib: expiry parsing ----------
    check("parse_expires 7d", sl.parse_expires("7d") == 7 * 86400)
    check("parse_expires 12h", sl.parse_expires("12h") == 12 * 3600)
    try:
        sl.parse_expires("nope"); ok = False
    except ValueError:
        ok = True
    check("parse_expires rejects garbage", ok)

    # wikilink scoping: within-set link → anchor; outside-set → plain text
    docs = [
        {"slug": "alpha", "title": "Alpha", "md": "# Alpha\nsee [[beta]] and [[ghost]]\n"},
        {"slug": "beta", "title": "Beta", "md": "# Beta\nhello\n"},
    ]
    htmldoc = sl.render_bundle(docs)
    check("render: self-contained (inline CSS, no external refs)",
          "<style>" in htmldoc and "http://" not in htmldoc and "<link" not in htmldoc)
    check("render: in-set wikilink → intra-doc anchor", 'href="#note-beta"' in htmldoc)
    check("render: out-of-set wikilink → plain text (no anchor)",
          "ghost" in htmldoc and 'href="#note-ghost"' not in htmldoc)

    # image size cap: resolver returns None over cap → alt text, not a data URI
    small = sl.render_note_html("![pic](x.png)\n", set(), image_resolver=lambda p: sl.data_uri(b"x", "image/png"))
    check("render: under-cap image inlined as data URI", "data:image/png;base64" in small)
    dropped = sl.render_note_html("![pic](big.png)\n", set(), image_resolver=lambda p: None)
    check("render: over-cap image dropped to alt text", "data:image" not in dropped and "pic" in dropped)

    tbl_md = (
        "| Role | Model |\n"
        "|------|-------|\n"
        "| **Default** | `gemma4:e4b` |\n"
        "| HU override | OpenEuroLLM |\n"
    )
    tbl_html = sl.render_note_html(tbl_md, set())
    check("render: GFM table → <table>", "<table>" in tbl_html and "<thead>" in tbl_html and "<tbody>" in tbl_html)
    check("render: table cells not raw pipe paragraphs",
          "<p>| Role |" not in tbl_html and "gemma4:e4b" in tbl_html)
    check("render: table-wrap for mobile scroll", "table-wrap" in sl.render_bundle(
        [{"slug": "t", "title": "T", "md": tbl_md}]))

    pipe_only = "| not a table without separator |\n"
    check("render: lone pipe line stays paragraph", "<table>" not in sl.render_note_html(pipe_only, set()))

    # ---------- sharelib: OPSX bundle pack/unpack + markdown bundling + capability-link parsing ----------
    md_b = sl.render_markdown_bundle(docs).encode("utf-8")
    html_b = htmldoc.encode("utf-8")
    packed = sl.pack_bundle(md_b, html_b)
    check("pack/unpack round-trips markdown + html", sl.unpack_bundle(packed) == (md_b, html_b))
    check("pack starts with OPSX magic", packed[:4] == sl.BUNDLE_MAGIC)

    # render_markdown_bundle: a single note is file-identical; a collection joins notes with a --- rule
    check("markdown bundle single note is file-identical",
          sl.render_markdown_bundle([docs[0]]) == docs[0]["md"])
    coll_md = sl.render_markdown_bundle(docs)
    check("markdown bundle collection joins notes with a --- rule",
          docs[0]["md"] in coll_md and docs[1]["md"] in coll_md and "\n---\n" in coll_md)

    # markdown_from_blob: single-arg OPSX unpack → the md half; non-OPSX bytes → ValueError('re-publish')
    md_one = sl.render_markdown_bundle([docs[0]]).encode("utf-8")
    packed_one = sl.pack_bundle(md_one, html_b)
    check("markdown_from_blob returns the md half of an OPSX blob",
          sl.markdown_from_blob(packed_one) == docs[0]["md"])
    try:
        sl.markdown_from_blob(b"not-an-opsx-blob!!"); ok, msg = False, ""
    except ValueError as e:
        ok, msg = True, str(e)
    check("markdown_from_blob non-OPSX → ValueError containing 're-publish'",
          ok and "re-publish" in msg, msg)

    # parse_share_link → (origin, share_id); tolerates .md, ignores stale #fragment; rejects garbage
    tok = "a" * 24
    o, sid_ = sl.parse_share_link(f"https://w.example/{tok}")
    check("parse_share_link: 24-char token URL", o == "https://w.example" and sid_ == tok, f"{o} {sid_}")
    o, sid_ = sl.parse_share_link(f"https://w.example/{tok}.md")
    check("parse_share_link: trailing .md agent link stripped", o == "https://w.example" and sid_ == tok, f"{o} {sid_}")
    o, sid_ = sl.parse_share_link(f"https://w.example/{tok}#stalekeyfragment")
    check("parse_share_link: stale #fragment ignored", o == "https://w.example" and sid_ == tok, f"{o} {sid_}")
    o, sid_ = sl.parse_share_link(f"https://w.example/{'b' * 10}")
    check("parse_share_link: legacy 10-char id", sid_ == "b" * 10, f"{o} {sid_}")
    try:
        sl.parse_share_link("not a url"); ok = False
    except ValueError:
        ok = True
    check("parse_share_link rejects garbage → ValueError", ok)

    # ---------- share: dry-run render (offline), confirm-gate, fake-transport bookkeeping ----------
    with tempfile.TemporaryDirectory() as td:
        h = Path(td) / "ops"; roots = Path(td) / "roots"
        (h / "wiki" / "notes").mkdir(parents=True); roots.mkdir()
        init_git(h)
        (h / "wiki" / "notes" / "alpha.md").write_text(
            "---\ntype: note\ntitle: Alpha\ntags: [pub]\n---\n# Alpha\nsee [[beta]] and [[ghost]]\n")
        (h / "wiki" / "notes" / "beta.md").write_text(
            "---\ntype: note\ntitle: Beta\ntags: [pub]\n---\n# Beta\nhi\n")

        # dry-run writes the HTML blob and never publishes / never touches the ledger
        outp = Path(td) / "alpha.html"
        r = run("share", "alpha", "--dry-run", "--out", str(outp), home=h)
        check("share dry-run exits 0", r.returncode == 0, r.stdout + r.stderr)
        check("share dry-run wrote HTML, no ledger",
              outp.exists() and not (h / ".share" / "ledger.json").exists())
        blob = outp.read_text() if outp.exists() else ""
        check("share dry-run: single note has no in-set links (beta/ghost plain)",
              "beta" in blob and "ghost" in blob and 'href="#note-' not in blob, blob[:200])

        # confirm-gate: no --yes → EXIT_CONFIRM(3)
        r = run("share", "alpha", home=h)
        check("share without --yes → exit 3 (needs-yes)", r.returncode == 3, r.stdout + r.stderr)

        # not-found slug
        r = run("share", "nonesuch", "--dry-run", home=h)
        check("share unknown slug → exit 4", r.returncode == 4, r.stdout + r.stderr)

        # full flow with fake transport: JSON contract + ledger + frontmatter + journal
        fenv = {"PLAINKEEP_SHARE_FAKE": "1"}
        r = run("share", "alpha", "--expires", "1d", "--yes", "--json", home=h, extra_env=fenv)
        check("share --yes (fake) exits 0", r.returncode == 0, r.stdout + r.stderr)
        env = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
        data = env.get("data", {})
        sid = data.get("id", "")
        url = data.get("url", "")
        agent_url = data.get("agent_url", "")
        check("share returns an id", bool(sid), r.stdout)
        check("share url is a plain capability URL (no #key fragment)",
              bool(url) and "#" not in url, r.stdout)
        check("share agent_url == url + '.md'", bool(url) and agent_url == url + ".md", r.stdout)
        check("share JSON has no 'encrypted' key", "encrypted" not in data, r.stdout)
        led = json.loads((h / ".share" / "ledger.json").read_text())
        entry = next((s for s in led["shares"] if s["id"] == sid), None)
        check("ledger records the share", entry is not None)
        check("ledger entry has no 'plain' key", entry is not None and "plain" not in entry, str(entry))
        check("frontmatter stamped with share:", "share:" in (h / "wiki" / "notes" / "alpha.md").read_text())
        jtext = ("".join(f.read_text() for f in (h / "journal").rglob("*.md"))
                 if (h / "journal").exists() else "")
        check("journal line appended for the share", "share" in jtext and sid in jtext, jtext[:120])

        # a second --dry-run publishes nothing and leaves the ledger untouched
        before = len(led["shares"])
        r = run("share", "beta", "--dry-run", "--json", home=h, extra_env=fenv)
        check("share --dry-run exits 0 (no publish)", r.returncode == 0, r.stdout + r.stderr)
        led_after = json.loads((h / ".share" / "ledger.json").read_text())
        check("share --dry-run adds no ledger entry", len(led_after["shares"]) == before, r.stdout)

        # list shows the active share
        r = run("share", "list", "--json", home=h)
        rows = [json.loads(l) for l in r.stdout.splitlines()]
        check("share list shows the active share",
              any(x.get("id") == sid and x.get("state") == "active" for x in rows[1:]), r.stdout)
        check("share list rows carry agent_url = url + '.md'",
              all(x.get("agent_url") == (x.get("url") or "") + ".md"
                  for x in rows[1:] if x.get("url")), r.stdout)

        # revoke: confirm-gate then mark revoked
        r = run("share", "revoke", sid, home=h)
        check("revoke without --yes → exit 3", r.returncode == 3, r.stdout + r.stderr)
        r = run("share", "revoke", sid, "--yes", home=h, extra_env=fenv)
        check("revoke --yes exits 0", r.returncode == 0, r.stdout + r.stderr)
        led = json.loads((h / ".share" / "ledger.json").read_text())
        check("ledger marks the share revoked", any(s["id"] == sid and s.get("revoked") for s in led["shares"]))

        # collection by tag renders both notes
        r = run("share", "collection", "pub", "--dry-run", "--out", str(Path(td) / "coll.html"),
                "--json", home=h)
        cenv = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
        check("collection selects both tagged notes",
              set(cenv.get("data", {}).get("notes", [])) == {"alpha", "beta"}, r.stdout)

        # ---------- transport: a REAL publish carries a named User-Agent (issue #2 part 1) ----------
        # Cloudflare bot-management 403s the default `Python-urllib/x.y` UA. Point the client at a
        # one-shot local server (no PLAINKEEP_SHARE_FAKE) and assert the PUT's UA + OPSX body. This
        # exercises the real _publish() path, header included.
        import http.server, threading
        cap = {}

        class _H(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                cap["ua"] = self.headers.get("User-Agent")
                cap["body"] = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(b'{"id":"testid","admin_token":"tok"}')

            def log_message(self, *a):  # keep the suite output clean
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        th = threading.Thread(target=srv.handle_request, daemon=True); th.start()
        (h / ".share").mkdir(exist_ok=True)
        (h / ".share" / "config.json").write_text(
            json.dumps({"endpoint": f"http://127.0.0.1:{srv.server_address[1]}"}))
        run("share", "alpha", "--yes", "--json", home=h)  # real transport, no FAKE
        th.join(timeout=5); srv.server_close()
        check("real publish sends a named User-Agent (Cloudflare 403 fix)",
              cap.get("ua") == "plainkeep-share/1.0", str(cap))
        check("real publish body is OPSX bundle (agent .md capable)",
              cap.get("body", b"")[:4] == b"OPSX", str(cap)[:80])

        # ---------- share pull: ?raw=1 fetch → wiki markdown (capability link) ----------
        pull_body = cap.get("body", b"")
        stored = {"body": pull_body}
        share_id = "abcdefghij"

        class _PullH(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if "raw=1" in self.path:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    self.wfile.write(stored["body"])
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format, *args):  # noqa: A002
                pass

        psrv = http.server.HTTPServer(("127.0.0.1", 0), _PullH)
        pth = threading.Thread(target=psrv.handle_request, daemon=True)
        pth.start()
        port = psrv.server_address[1]
        human = f"http://127.0.0.1:{port}/{share_id}"
        r = run("share", "pull", human, home=h)
        pth.join(timeout=5)
        psrv.server_close()
        check("pull capability share prints wiki markdown",
              r.returncode == 0 and "# Alpha" in r.stdout, r.stdout[:120] + r.stderr[:120])

        # ---------- sweep share hygiene: expired + edited-since warnings ----------
        import time as _t
        (h / ".share").mkdir(exist_ok=True)
        now = int(_t.time())
        ledger = {"shares": [
            {"id": "expd", "kind": "note", "key": "alpha", "expires_ts": now - 86400,
             "created_ts": now - 200000, "note_paths": ["wiki/notes/alpha.md"]},
            {"id": "editd", "kind": "note", "key": "beta", "expires_ts": now + 86400,
             "created_ts": now - 100000, "note_paths": ["wiki/notes/beta.md"]},
        ]}
        (h / ".share" / "ledger.json").write_text(json.dumps(ledger))
        r = run("sweep", "--json", home=h, extra_env={"PLAINKEEP_SWEEP_HOME": str(roots)})
        head = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
        srows = [json.loads(l) for l in r.stdout.splitlines()[1:]]
        check("sweep reports 2 share warnings", head.get("share_warnings") == 2, r.stdout)
        check("sweep flags the expired share", any(x.get("action") == "expired" for x in srows), r.stdout)
        check("sweep flags the edited-since share", any(x.get("action") == "edited" for x in srows), r.stdout)

    # ---------- backup: bare nag unchanged, status, bundle, drill, init, run-cloud gate ----------
    with tempfile.TemporaryDirectory() as td:
        h = Path(td) / "ops"; roots = Path(td) / "roots"
        (h / "wiki").mkdir(parents=True)
        (roots / "files").mkdir(parents=True)
        init_git(h)
        (h / "wiki" / "index.md").write_text("# index\n")
        git(h, "add", "-A"); git(h, "commit", "-qm", "init")

        # bare nag still works (byte-compatible): committed but no upstream → at risk, exit 1
        r = run("backup", home=h, roots=roots)
        check("bare backup nag: no upstream → exit 1", r.returncode == 1 and "no upstream" in r.stdout,
              r.stdout + r.stderr)

        # status: unconfigured → exit 1, reports it
        r = run("backup", "status", "--json", home=h, roots=roots)
        check("status unconfigured → exit 1", r.returncode == 1, r.stdout + r.stderr)
        head = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
        check("status flags at_risk when unconfigured", head.get("at_risk") is True, r.stdout)

        # run --target cloud without --yes → exit 3 (confirm), before touching restic/config
        r = run("backup", "run", "--target", "cloud", home=h, roots=roots)
        check("backup run cloud without --yes → exit 3", r.returncode == 3, r.stdout + r.stderr)

        # drill without --yes → exit 3
        r = run("backup", "drill", home=h, roots=roots)
        check("backup drill without --yes → exit 3", r.returncode == 3, r.stdout + r.stderr)
        # drill --yes unconfigured (or no restic) → clean error, not a crash
        r = run("backup", "drill", "--yes", home=h, roots=roots)
        check("backup drill --yes unconfigured → clean error (exit 1)", r.returncode == 1, r.stdout + r.stderr)

        # init without --yes → exit 3
        r = run("backup", "init", home=h, roots=roots)
        check("backup init without --yes → exit 3", r.returncode == 3, r.stdout + r.stderr)
        # init --yes → config with op:// references only + rendered plist
        r = run("backup", "init", "--yes", "--local-repo", "/Volumes/Backup/restic-plainkeep",
                "--cloud-repo", "b2:mybucket:plainkeep", home=h, roots=roots)
        check("backup init --yes exits 0", r.returncode == 0, r.stdout + r.stderr)
        cfg = json.loads((h / ".backup" / "config.json").read_text())
        refs = json.dumps(cfg)
        check("config stores op:// references (never resolved)", "op://" in refs)
        check("config resolves NO secret value", "password\":" not in refs.replace("password_ref", ""))
        plist = (h / ".backup" / "com.plainkeep.backup.cloud.plist").read_text()
        check("plist renders launchd job invoking restic directly",
              "restic backup" in plist and "op read" in plist and "com.plainkeep.backup.cloud" in plist)
        check("plist is NOT auto-installed (LaunchAgents untouched)",
              not (Path(td) / "Library").exists())

        # status now configured: local repo path unreachable/empty → still exit 1, reports stale/hint
        r = run("backup", "status", "--json", home=h, roots=roots)
        check("status configured-but-unbacked → exit 1", r.returncode == 1, r.stdout + r.stderr)

    # ---------- backup bundle: valid git bundles + retention (fully offline) ----------
    with tempfile.TemporaryDirectory() as td:
        h = Path(td) / "ops"; roots = Path(td) / "roots"
        (h / "wiki").mkdir(parents=True)
        init_git(h)
        (h / "wiki" / "index.md").write_text("# index\n")
        git(h, "add", "-A"); git(h, "commit", "-qm", "init")
        # a remote-less work repo — the exact hole bundle closes
        wrepo = roots / "work" / "labs" / "demo"; wrepo.mkdir(parents=True)
        init_git(wrepo)
        (wrepo / "readme.md").write_text("# demo\n")
        git(wrepo, "add", "-A"); git(wrepo, "commit", "-qm", "init")

        r = run("backup", "bundle", "--dry-run", "--json", home=h, roots=roots)
        head = json.loads(r.stdout.splitlines()[0]) if r.stdout.strip() else {}
        check("bundle dry-run writes nothing but plans repos", head.get("dry_run") is True
              and head.get("bundled") >= 2 and not (roots / "files" / "backups").exists(), r.stdout)

        r = run("backup", "bundle", "--json", home=h, roots=roots)
        check("bundle exits 0", r.returncode == 0, r.stdout + r.stderr)
        bdir = roots / "files" / "backups" / "bundles"
        bundles = sorted(bdir.glob("*.bundle")) if bdir.exists() else []
        check("bundle produced >=2 .bundle files (plainkeep + work repo)", len(bundles) >= 2, str(bundles))
        ok = all(git(h, "bundle", "verify", str(b)).returncode == 0 for b in bundles)
        check("every produced bundle is a valid git bundle", ok)

        # retention: run 4 more times with keep=2 → at most 2 per repo
        for _ in range(4):
            run("backup", "bundle", home=h, roots=roots, extra_env={"PLAINKEEP_BUNDLE_KEEP": "2"})
        plainkeep_bundles = list(bdir.glob("plainkeep-*.bundle"))
        check("retention keeps last N per repo", len(plainkeep_bundles) <= 2, str(plainkeep_bundles))

    print(f"\n{BOLD}Backup family + share (Part 5.1/5.2) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<52}" + (f" {DIM}{detail.strip()[:90]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
