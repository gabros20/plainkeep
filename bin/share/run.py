#!/usr/bin/env python3
"""
plainkeep share <slug> | collection <tag> | list | pull <url> | revoke <id> | init — capability-URL,
confirm-gated sharing (proposal Part 5.2). Render a note (or a tag collection) to ONE self-contained
HTML blob plus its raw markdown LOCALLY, pack them into a plaintext OPSX bundle, PUT that bundle to a
vendored Cloudflare Worker + KV, and hand back ONE link. The verb's output is a DRAFT link — the
human sends it.

The model is a capability URL: there is NO encryption. The worker stores the bundle in the clear and
serves `https://<worker>/<token>` as HTML to browsers and `<worker>/<token>.md` as raw markdown to
agents/LLMs. Secrecy = an unguessable 24-char token + a TTL; whoever holds the link can read it, so
treat the link as the secret. The worker requires an `X-Publish-Token` header (its PUBLISH_TOKEN
secret, mirrored in `.share/config.json` as `publish_token`) so only this vault can publish.

Governance: risk safe_write at the surface so `list` stays free, but every TRANSMITTING subaction
(share/collection/revoke, and `init` deploy) self-gates `--yes` → EXIT_CONFIRM(3). Publishing the
note off-machine IS a transmission; the transport is factored into _publish()/_revoke_remote() and is
NEVER exercised by tests (PLAINKEEP_SHARE_FAKE short-circuits it deterministically). `--dry-run` renders
locally and stops before the PUT. Fallback for the unprovisioned: `--gist` (secret gist via
`gh gist create`, confirm-gated).
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import output, paths, sharelib, vaultio  # noqa: E402

GREEN, YEL, DIM, RESET = "\033[32m", "\033[33m", "\033[2m", "\033[0m"

# Cloudflare bot-management 403s the default `Python-urllib/x.y` User-Agent (a PUT that succeeds
# from curl fails from urllib for this reason alone). A named UA passes the edge.
UA = "plainkeep-share/1.0"

SHARE_DIR = paths.PLAINKEEP_HOME / ".share"
CONFIG = SHARE_DIR / "config.json"
LEDGER = SHARE_DIR / "ledger.json"
IMG_CAP = int(os.environ.get("PLAINKEEP_SHARE_IMG_CAP", str(512 * 1024)))  # inline-image size cap (bytes)
IMG_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}


# --------------------------------------------------------------------------- config + ledger I/O

def _config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _merge_config(**updates) -> dict:
    """Merge `updates` into `.share/config.json`, PRESERVING existing keys (endpoint, publish_token)."""
    cfg = _config()
    cfg.update({k: v for k, v in updates.items() if v is not None})
    vaultio.mkdir(SHARE_DIR)
    vaultio.write_text(CONFIG, json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return cfg


def _ledger() -> dict:
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except Exception:
        return {"shares": []}


def _write_ledger(led: dict) -> None:
    vaultio.mkdir(SHARE_DIR)
    vaultio.write_text(LEDGER, json.dumps(led, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _notes() -> dict:
    if not paths.WIKI.exists():
        return {}
    return {p.stem: p for p in sorted(paths.WIKI.rglob("*.md")) if p.suffix == ".md"}


def _image_resolver(base: Path):
    def resolve(path: str):
        for root in (base, paths.FILES_ROOT, paths.WIKI):
            cand = (root / path).resolve()
            try:
                cand.relative_to(paths.FILES_ROOT.resolve())
            except Exception:
                try:
                    cand.relative_to(paths.PLAINKEEP_HOME.resolve())
                except Exception:
                    continue
            if cand.is_file() and cand.stat().st_size <= IMG_CAP:
                mime = IMG_MIME.get(cand.suffix.lower())
                if mime:
                    return sharelib.data_uri(cand.read_bytes(), mime)
        return None
    return resolve


# --------------------------------------------------------------------------- transport (never in tests)

def _fake() -> bool:
    return os.environ.get("PLAINKEEP_SHARE_FAKE", "").lower() in ("1", "true", "yes")


def _publish(endpoint: str, body: bytes, ttl: int, publish_token: str = "") -> dict:
    """PUT the plaintext OPSX bundle to the worker; returns {id, admin_token}. Deterministic
    no-network stub under PLAINKEEP_SHARE_FAKE (the ONLY path tests take)."""
    if _fake():
        import hashlib
        h = hashlib.sha256(body).hexdigest()[:16]
        return {"id": h, "admin_token": "fake-" + h}
    headers = {"Content-Type": "application/octet-stream",
               "X-Expire-Seconds": str(ttl), "User-Agent": UA}
    if publish_token:
        headers["X-Publish-Token"] = publish_token
    req = urllib.request.Request(endpoint.rstrip("/") + "/", data=body, method="PUT", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:  # pragma: no cover - never run in tests
        return json.loads(r.read().decode("utf-8"))


def _revoke_remote(endpoint: str, sid: str, token: str) -> None:
    if _fake():
        return
    req = urllib.request.Request(f"{endpoint.rstrip('/')}/{sid}", method="DELETE",
                                 headers={"X-Admin-Token": token, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30):  # pragma: no cover - never run in tests
        pass


def _stamp_frontmatter(path: Path, url: str | None) -> None:
    """Splice/replace the `share:` line in a note's frontmatter; `url=None` removes it.

    Removal is what revoke needs: a note that advertises a URL returning 404 is worse than one
    advertising nothing, and the stale value is committed like any other content change.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    if not lines or lines[0].strip() != "---":
        return
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return
    body = [ln for ln in lines[1:end] if not ln.startswith("share:")]
    if url is not None:
        body.append(f"share: {url}")
    elif len(body) == len(lines[1:end]):
        return                                   # nothing to strip — leave the file untouched
    vaultio.write_text(path, "\n".join(["---", *body, "---", *lines[end + 1:]]) + "\n", encoding="utf-8")


def _unstamp_entry(entry: dict) -> list[str]:
    """Strip the `share:` field from every note an entry published. Returns the paths cleared."""
    cleared = []
    for rel in entry.get("note_paths") or entry.get("notes") or []:
        p = paths.PLAINKEEP_HOME / rel
        if not p.is_file():
            continue
        before = p.read_text(encoding="utf-8")
        _stamp_frontmatter(p, None)
        if p.read_text(encoding="utf-8") != before:
            cleared.append(rel)
    return cleared


# --------------------------------------------------------------------------- share / collection

def _collect(argv):
    """Return (kind, key, [(slug, path)]). Single note by slug, or a collection by tag."""
    notes = _notes()
    if argv and argv[0] == "collection":
        tag = argv[1] if len(argv) > 1 else ""
        if not tag:
            output.fail(output.EXIT_USAGE, "usage: plainkeep share collection <tag>", verb="share")
        tag = tag.lstrip("#")
        sel = [(s, p) for s, p in notes.items() if tag in paths.fm_list(p, "tags")]
        return "collection", tag, sel
    slug = argv[0] if argv else ""
    p = notes.get(slug)
    if not p:
        output.fail(output.EXIT_NOT_FOUND, f"no note '{slug}'", verb="share")
    return "note", slug, [(slug, p)]


VALUE_FLAGS = ("--expires", "--out")


def _positional(argv):
    """Drop flags AND the values that follow value-flags, leaving the subcommand/slug tokens."""
    pos, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a in VALUE_FLAGS:
            skip = True
            continue
        if a.startswith("-"):
            continue
        pos.append(a)
    return pos


def cmd_share(argv):
    dry = "--dry-run" in argv
    yes = ("--yes" in argv) or ("-y" in argv)
    gist = "--gist" in argv
    expires = argv[argv.index("--expires") + 1] if "--expires" in argv else "7d"
    out = argv[argv.index("--out") + 1] if "--out" in argv else None
    try:
        ttl = sharelib.parse_expires(expires)
    except ValueError as e:
        output.fail(output.EXIT_USAGE, str(e), verb="share")

    pos = _positional(argv)
    kind, key, sel = _collect(pos)
    if not sel:
        output.fail(output.EXIT_NOT_FOUND, f"no notes match '{key}'", verb="share")

    docs = [{"slug": s, "title": paths.title_of(p), "md": p.read_text(encoding="utf-8")}
            for s, p in sel]
    base = sel[0][1].parent
    html_bytes = sharelib.render_bundle(docs, image_resolver=_image_resolver(base)).encode("utf-8")
    md_bytes = sharelib.render_markdown_bundle(docs).encode("utf-8")
    body = sharelib.pack_bundle(md_bytes, html_bytes)

    if dry:
        if out:
            Path(out).write_bytes(html_bytes)  # rendered HTML for inspection
        data = {"kind": kind, "key": key, "notes": [d["slug"] for d in docs],
                "bytes": len(body), "expires_seconds": ttl, "out": out}
        return output.emit(data, "share", human=lambda _:
                           f"{DIM}dry-run{RESET}: rendered {len(docs)} note(s), {len(body)} bytes,"
                           f" not published.")

    if gist:
        return _gist(docs, yes)

    if not yes:
        output.fail(output.EXIT_CONFIRM,
                    f"publishing the note off-machine is a transmission ({len(body)} bytes)",
                    hint=f"re-run: plainkeep share {' '.join(pos)} --yes", verb="share")

    cfg = _config()
    endpoint = cfg.get("endpoint")
    if not endpoint and not _fake():
        output.fail(output.EXIT_UNEXPECTED, "no share endpoint configured",
                    hint="run: plainkeep share init  (or use --gist)", verb="share")
    res = _publish(endpoint or "https://fake.invalid", body, ttl, cfg.get("publish_token", ""))
    sid, token = res["id"], res.get("admin_token", "")
    pub_origin = (endpoint or "https://fake.invalid").rstrip("/")
    url = f"{pub_origin}/{sid}"
    agent_url = url + ".md"

    entry = {"id": sid, "kind": kind, "key": key, "url": url, "admin_token": token,
             "notes": [d["slug"] for d in docs],
             "note_paths": [str(p.relative_to(paths.PLAINKEEP_HOME)) for _, p in sel],
             "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "created_ts": int(datetime.now(timezone.utc).timestamp()),
             "expires_ts": int(datetime.now(timezone.utc).timestamp()) + ttl}
    led = _ledger()
    led["shares"].append(entry)
    _write_ledger(led)
    for _, p in sel:
        _stamp_frontmatter(p, url)
    paths.append_journal(f"share {kind} {key} -> {sid}")

    data = {"id": sid, "url": url, "agent_url": agent_url, "kind": kind, "key": key,
            "expires_ts": entry["expires_ts"]}
    return output.emit(data, "share", human=lambda _:
                       f"{GREEN}shared{RESET} {kind} {key}\n"
                       f"  {url}\n"
                       f"  {YEL}agents / LLMs: {agent_url}   (raw markdown — paste into any chat or "
                       f"coding agent){RESET}\n"
                       f"  {DIM}revoke: plainkeep share revoke {sid} --yes{RESET}")


def _gist(docs, yes):
    if not yes:
        output.fail(output.EXIT_CONFIRM, "creating a secret gist is a transmission",
                    hint="re-run with --gist --yes", verb="share")
    import shutil
    if not shutil.which("gh"):
        output.fail(output.EXIT_UNEXPECTED, "gh not installed", hint="brew install gh", verb="share")
    # pragma: no cover - confirm-gated, never in tests
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(sharelib.render_bundle(docs))
        tmp = f.name
    r = subprocess.run(["gh", "gist", "create", tmp], capture_output=True, text=True)
    if r.returncode != 0:
        output.fail(output.EXIT_UNEXPECTED, "gh gist create failed", hint=r.stderr.strip(), verb="share")
    return output.emit({"url": r.stdout.strip(), "kind": "gist"}, "share",
                       human=lambda d: f"{GREEN}gist{RESET}: {d['url']}")


# --------------------------------------------------------------------------- list / revoke

def cmd_list():
    led = _ledger()
    now = int(datetime.now(timezone.utc).timestamp())
    rows = []
    for s in led["shares"]:
        if s.get("revoked"):
            state = "revoked"
        elif s.get("expires_ts", 0) < now:
            state = "expired"
        else:
            state = "active"
        url = s.get("url")
        rows.append({"id": s["id"], "kind": s.get("kind"), "key": s.get("key"),
                     "state": state, "expires_ts": s.get("expires_ts"), "url": url,
                     "agent_url": (url + ".md") if url else None})

    def render(rs):
        if not rs:
            return "no shares yet — `plainkeep share <slug> --yes`"
        out = [f"{len(rs)} share(s):"]
        for r in rs:
            out.append(f"  {r['id']:<18} {r['state']:<8} {r['kind']}:{r['key']}")
        return "\n".join(out)

    return output.emit_rows(rows, "share", human=render, header={"count": len(rows)})


def cmd_revoke(argv):
    sid = next((a for a in argv if not a.startswith("-")), "")
    yes = ("--yes" in argv) or ("-y" in argv)
    if not sid:
        output.fail(output.EXIT_USAGE, "usage: plainkeep share revoke <id> --yes", verb="share")
    led = _ledger()
    entry = next((s for s in led["shares"] if s["id"] == sid), None)
    if not entry:
        output.fail(output.EXIT_NOT_FOUND, f"no share '{sid}' in ledger", verb="share")
    if not yes:
        output.fail(output.EXIT_CONFIRM, f"revoking '{sid}' deletes the published blob",
                    hint=f"re-run: plainkeep share revoke {sid} --yes", verb="share")
    cfg = _config()
    _revoke_remote(cfg.get("endpoint", "https://fake.invalid"), sid, entry.get("admin_token", ""))
    entry["revoked"] = True
    entry["revoked_ts"] = int(datetime.now(timezone.utc).timestamp())
    _write_ledger(led)
    cleared = _unstamp_entry(entry)   # the note must stop advertising a URL that now 404s
    paths.append_journal(f"share revoke {sid}")
    return output.emit({"id": sid, "revoked": True, "unstamped": cleared}, "share",
                       human=lambda _: f"{GREEN}revoked{RESET} {sid}"
                                       + (f" ({len(cleared)} note(s) unstamped)" if cleared else ""))


# --------------------------------------------------------------------------- prune

def cmd_prune(argv):
    """Drop ledger entries whose share is gone (revoked, or past its expiry).

    The ledger is append-only in practice: `revoke` marks an entry but nothing removes one, and an
    expired share just stops resolving. `share list` therefore reports links that cannot be fetched,
    and the entries keep holding an `admin_token` and `key` for blobs that no longer exist.
    """
    yes = ("--yes" in argv) or ("-y" in argv)
    led = _ledger()
    now = int(datetime.now(timezone.utc).timestamp())

    def dead(s: dict) -> str | None:
        if s.get("revoked"):
            return "revoked"
        exp = s.get("expires_ts")
        if exp and int(exp) < now:
            return "expired"
        return None

    doomed = [(s, dead(s)) for s in led["shares"] if dead(s)]
    keep = [s for s in led["shares"] if not dead(s)]
    listing = [{"id": s.get("id"), "reason": why,
                "notes": s.get("note_paths") or s.get("notes") or []} for s, why in doomed]

    if not doomed:
        return output.emit({"pruned": 0, "kept": len(keep), "entries": []}, "share",
                           human=lambda _: f"nothing to prune ({len(keep)} live share(s))")
    if not yes:
        # A dry run is a read, so it needs no --yes; the WRITE is what is gated.
        return output.emit({"pruned": 0, "kept": len(keep), "would_prune": listing}, "share",
                           human=lambda _: f"would prune {len(doomed)} dead entr(y/ies), keeping "
                                           f"{len(keep)}:\n  "
                                           + "\n  ".join(f"{e['id']} — {e['reason']}" for e in listing)
                                           + "\n(re-run with --yes to write)")

    cleared = []
    for s, _why in doomed:
        cleared += _unstamp_entry(s)     # a pruned entry must not leave notes pointing at a dead URL
    led["shares"] = keep
    _write_ledger(led)
    paths.append_journal(f"share prune ({len(doomed)} dead entr(y/ies))")
    return output.emit({"pruned": len(doomed), "kept": len(keep),
                        "entries": listing, "unstamped": cleared}, "share",
                       human=lambda _: f"{GREEN}pruned{RESET} {len(doomed)} dead entr(y/ies), "
                                       f"{len(keep)} live remain"
                                       + (f" ({len(cleared)} note(s) unstamped)" if cleared else ""))


# --------------------------------------------------------------------------- init

# THE CODE IS ENGINE-OWNED; THE CONFIG IS VAULT-OWNED, and since Phase 2 Task 2 those are two
# different trees. `bin/share/worker/` ships with the engine (`enginetree.OWNED_TREES` names `bin`,
# and `verify()` names `worker.js` outright) and an installed engine is sealed 0555 — but
# `wrangler.toml` is PER-VAULT data, which `script/engine.txt` has always said ("bin/share/worker/
# wrangler.toml is NOT in upstream (per-vault)"). Under Phase 1 both halves sat inside the vault and
# the distinction cost nothing. After the engine moved out, writing the config beside its own source
# meant writing into the sealed engine: the path-wall refused it (correctly — the path escapes the
# three roots) and `share init --yes`, the only way to configure the worker, could not run at all.
#
# So the two halves are anchored separately: the worker source stays where the engine put it, and the
# config joins `.share/config.json` in the vault it belongs to. `wrangler` is told where to find it
# with `--config`; nothing is written into the engine.
WORKER_DIR = Path(__file__).resolve().parent / "worker"
WRANGLER_TOML = SHARE_DIR / "wrangler.toml"
WRANGLER_EXAMPLE = WORKER_DIR / "wrangler.toml.example"


def _worker_entry() -> Path:
    """Absolute path to `worker.js`, preferred through the `current` symlink.

    `main` cannot be relative. wrangler resolves it against the CONFIG FILE's directory, not the
    process cwd — and since Task 2 the config lives in the vault (`.share/`) while `worker.js`
    ships with the engine, so a relative `main` resolves to `<vault>/.share/worker.js`, which
    never exists. Pinning through `current` rather than the concrete version keeps a vault's
    config valid across engine upgrades; it is only used when `current` really does point at the
    running engine, so a vault pinned to an older engine still gets a correct concrete path.
    """
    concrete = (WORKER_DIR / "worker.js").resolve()
    try:
        from lib import enginetree
        via_current = enginetree.current_link() / "bin" / "share" / "worker" / "worker.js"
        if via_current.resolve() == concrete:
            return via_current
    except Exception:
        pass
    return concrete


_MAIN_RE = re.compile(r'^main\s*=\s*"([^"]*)"', re.M)


def _pin_worker_entry(text: str) -> str:
    """Rewrite `main` to the absolute worker.js when it does not already resolve to a file."""
    m = _MAIN_RE.search(text)
    entry = _worker_entry()
    if m:
        cur = m.group(1)
        if cur and (WRANGLER_TOML.parent / cur).exists():
            return text                      # already resolvable from the config's dir — leave it
        return text[:m.start()] + f'main = "{entry}"' + text[m.end():]
    return text.rstrip("\n") + f'\nmain = "{entry}"\n'


def _ensure_wrangler_toml() -> None:
    if WRANGLER_TOML.is_file():
        # Repair configs written before `main` was pinned — otherwise every vault that already
        # copied the example stays undeployable, and `init` is the only path that could fix it.
        cur = WRANGLER_TOML.read_text(encoding="utf-8")
        fixed = _pin_worker_entry(cur)
        if fixed != cur:
            vaultio.write_text(WRANGLER_TOML, fixed, encoding="utf-8")
        return
    if not WRANGLER_EXAMPLE.is_file():
        output.fail(output.EXIT_UNEXPECTED, "missing wrangler.toml.example in worker dir",
                    hint=f"reinstall the engine; expected {WRANGLER_EXAMPLE}", verb="share")
    vaultio.mkdir(SHARE_DIR)
    vaultio.write_text(WRANGLER_TOML,
                       _pin_worker_entry(WRANGLER_EXAMPLE.read_text(encoding="utf-8")),
                       encoding="utf-8")


def _worker_has_publish_token() -> bool:
    """Whether the DEPLOYED worker actually holds a PUBLISH_TOKEN secret.

    `wrangler secret list --config <toml>` prints a JSON array of {name, type}. Any failure to
    determine this returns False: re-running `secret put` is idempotent and harmless, whereas a
    wrong True is what leaves an endpoint unauthenticated.
    """
    import subprocess
    try:
        r = subprocess.run(["wrangler", "secret", "list", "--config", str(WRANGLER_TOML)],
                           cwd=str(WORKER_DIR), capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return False
        start = r.stdout.find("[")
        if start < 0:
            return False
        return any(s.get("name") == "PUBLISH_TOKEN"
                   for s in json.loads(r.stdout[start:]) if isinstance(s, dict))
    except Exception:
        return False


def _wrangler_kv_configured() -> bool:
    if not WRANGLER_TOML.is_file():
        return False
    return "PASTE_KV_NAMESPACE_ID_HERE" not in WRANGLER_TOML.read_text(encoding="utf-8")


def cmd_pull(argv):
    """Fetch the plaintext OPSX bundle from the worker and print its wiki markdown."""
    pos = [a for a in argv if not a.startswith("-")]
    url = pos[0] if pos else ""
    out = None
    if "--out" in argv:
        out = argv[argv.index("--out") + 1]
    if not url:
        output.fail(output.EXIT_USAGE,
                    "usage: plainkeep share pull <https://<worker>/<id>> [--out file.md]",
                    verb="share")
    try:
        origin, sid = sharelib.parse_share_link(url)
    except ValueError as e:
        output.fail(output.EXIT_USAGE, str(e), verb="share")
    try:
        md = sharelib.pull_markdown_from_url(url)
    except sharelib.SharePullError as e:
        # "expired"/"revoked" → the share is simply gone (404). The legacy-410 re-publish hint is a
        # different situation (the share exists but is a dead encrypted blob) → EXIT_UNEXPECTED.
        code = output.EXIT_NOT_FOUND if "expired" in str(e) or "revoked" in str(e) else output.EXIT_UNEXPECTED
        output.fail(code, str(e), verb="share")
    except ValueError as e:
        output.fail(output.EXIT_UNEXPECTED, str(e), verb="share")
    if out:
        Path(out).write_text(md, encoding="utf-8")
    data = {"id": sid, "bytes": len(md), "out": out}
    if output.json_mode():
        data["markdown"] = md

    def _human(_):
        if out:
            return f"{DIM}pulled {len(md)} bytes → {out}{RESET}"
        sys.stdout.write(md)

    return output.emit(data, "share", human=_human)


def cmd_init(argv):
    yes = ("--yes" in argv) or ("-y" in argv)
    endpoint = argv[argv.index("--endpoint") + 1] if "--endpoint" in argv else None
    if endpoint:
        _merge_config(endpoint=endpoint)  # merge, never clobber an existing publish_token
    steps = [
        # The engine tree is READ-ONLY, so none of these run inside it: the config is copied OUT of
        # the engine's example and into the vault, and every wrangler call is pointed back at it.
        f"cp {WRANGLER_EXAMPLE} {WRANGLER_TOML}   # once per vault; lives in the vault, not the engine",
        f"wrangler kv namespace create PLAINKEEP_SHARE --config {WRANGLER_TOML}",
        f"# paste the namespace id into {WRANGLER_TOML}, then:",
        f"cd {WORKER_DIR} && wrangler deploy --config {WRANGLER_TOML}",
        'python3 -c "import secrets; print(secrets.token_urlsafe(24))" | wrangler secret put '
        f"PUBLISH_TOKEN --config {WRANGLER_TOML}   # then put the same value in .share/config.json "
        "as publish_token",
        "plainkeep share init --endpoint https://<your-worker>.workers.dev",
        "# canonical flow: once wrangler.toml has its KV id, just re-run `plainkeep share init --yes` —",
        "# it deploys, generates + sets PUBLISH_TOKEN, and mirrors it into .share/config.json.",
    ]
    if not yes:
        return output.emit({"deployed": False, "worker": str(WORKER_DIR), "steps": steps},
                           "share", human=lambda _:
                           "deploy the share worker once (confirm with --yes to run wrangler):\n  "
                           + "\n  ".join(steps))
    # pragma: no cover - confirm-gated wrangler deploy, never in tests
    import secrets, shutil, subprocess
    if not shutil.which("wrangler"):
        output.fail(output.EXIT_UNEXPECTED, "wrangler not installed",
                    hint="npm i -g wrangler", verb="share")
    _ensure_wrangler_toml()
    if not _wrangler_kv_configured():
        output.fail(output.EXIT_UNEXPECTED,
                    "wrangler.toml still has placeholder KV id",
                    hint=f"edit {WRANGLER_TOML} after `wrangler kv namespace create PLAINKEEP_SHARE`", verb="share")
    # `--config` on every call: the cwd is the engine's worker directory (that is where worker.js
    # is), and the config it would otherwise pick up beside it does not exist there any more.
    r = subprocess.run(["wrangler", "deploy", "--config", str(WRANGLER_TOML)], cwd=str(WORKER_DIR))
    deployed = r.returncode == 0
    # Ask the WORKER what secrets it has — not `.share/config.json`. The local mirror says what this
    # vault last generated, which is a different question: after a rename (or any deploy to a new
    # worker name) the config still carries the old worker's token while the new worker has none.
    # Trusting the mirror skipped `secret put` exactly when it was most needed, and — before the
    # worker began failing closed — published an open endpoint while reporting "PUBLISH_TOKEN set".
    token_set = deployed and _worker_has_publish_token()
    if deployed and not token_set:
        # Reuse the vault's existing token when it has one, so a redeploy under a new worker name
        # keeps `.share/config.json` valid instead of silently invalidating it.
        token = _config().get("publish_token") or secrets.token_urlsafe(24)
        sr = subprocess.run(["wrangler", "secret", "put", "PUBLISH_TOKEN",
                             "--config", str(WRANGLER_TOML)], cwd=str(WORKER_DIR),
                            input=token, text=True)
        if sr.returncode == 0:
            _merge_config(publish_token=token)  # preserve endpoint; mirror the worker secret locally
            token_set = True
    return output.emit({"deployed": deployed, "token_set": token_set}, "share",
                       human=lambda d: f"wrangler deploy exit {r.returncode}"
                                       + (f"; PUBLISH_TOKEN {'set' if token_set else 'NOT set'}"
                                          if deployed else ""))


def main(argv):
    _, argv = output.parse_argv(argv)
    action = argv[0] if argv else ""
    if action == "list":
        return cmd_list()
    if action == "revoke":
        return cmd_revoke(argv[1:])
    if action == "prune":
        return cmd_prune(argv[1:])
    if action == "init":
        return cmd_init(argv[1:])
    if action == "pull":
        return cmd_pull(argv[1:])
    if action == "collection":
        return cmd_share(argv)
    if not action or action.startswith("-"):
        output.fail(output.EXIT_USAGE,
                    "usage: plainkeep share <slug> [--expires 7d] | collection <tag> | list | pull <url> "
                    "| revoke <id> | prune [--yes] | init",
                    verb="share")
    return cmd_share(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
