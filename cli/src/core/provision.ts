// PROVISIONING FROM THE COMPILED CORE — the port of `bin/lib/provision.py` (Phase 2 Task 4a).
//
// WHY THIS EXISTS AT ALL, since the Python module already does the job: on a machine with **no
// system `python3`**, the Python module cannot run. Nothing can. That is not a corner case for this
// binary — it is the state a fresh install is in before it has been provisioned, and the compiled
// core is the only thing on the machine that can act in it. So the bootstrap has to be reachable
// from here: `plainkeep-core --core-provision uv` downloads the pinned uv, and `--core-provision
// sync` runs `uv sync --frozen` against the delivered project, which is what CREATES the interpreter
// the rest of the engine runs on.
//
// It is a PORT, in the ADR-018 sense — same decisions, same file read (`bin/lib/uvpin.json`), same
// refusal text — and `test/run_provision.py` compares the two implementations' answers rather than
// trusting that they agree. Read `bin/lib/provision.py`'s header for the five decisions; they are
// not restated here, because two copies of a rationale is how two implementations drift.
//
// One difference worth naming rather than hiding: the download here goes through `fetch`, which in
// bun carries its own CA roots. The Python side has to fall back to `curl` when the interpreter has
// no CA bundle (a stock python.org CPython on macOS), and that asymmetry is real — this path simply
// does not have the problem.
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

// enginetree.PROVISION_DIR — the one writable directory in a sealed engine tree. Spelled here as a
// constant rather than derived, for the reason every ported constant in this directory is: the
// authority is the Python module, and the parity suite is what proves the two spellings agree.
export const PROVISION_DIR = "tools";
export const VENV_DIRNAME = "venv";
export const PYTHON_DIRNAME = "python";
export const PIN_REL = path.join("bin", "lib", "uvpin.json");

// enginetree.OWNED_TREES / OWNED_FILES — what an installed engine claims as its own code, and so
// what "the tree is intact" is a statement ABOUT. Ported for the same reason as PROVISION_DIR, and
// with the same standard of proof: the parity suite asserts the two spellings agree, because a tree
// this side forgets to walk is a tree an attacker can add a file to without the core noticing.
// Posix-separated: these are manifest keys, not host paths.
export const OWNED_TREES = ["bin", "templates/verb", "frontends/raycast",
                            "skills/operate-plainkeep"] as const;
export const OWNED_FILES = ["VERSION", "plainkeep", "pyproject.toml", "uv.lock"] as const;

export type UvPin = {
  version: string;
  url_template: string;
  member_template?: string;
  artifacts: Record<string, string>;
};

export class ProvisionRefusal extends Error {}

// A refusal about the TREE rather than about the download — its own class because it carries its own
// exit code (5, EXIT_DENY: a policy refusal), and because `cli.ts` must not report "the engine was
// tampered with" as the generic exit 1 every other provisioning refusal uses.
export class TamperRefusal extends ProvisionRefusal {}

export function toolsDir(engineRoot: string): string {
  return path.join(engineRoot, PROVISION_DIR);
}

export function projectEnv(engineRoot: string): string {
  return path.join(toolsDir(engineRoot), VENV_DIRNAME);
}

// THE PINNED ENGINE INTERPRETER — and the whole point of ADR-013's carried fix.
//
// `dispatch.ts`'s O_NONBLOCK helper used to spawn whatever `pickPython()` answered, which falls back
// to a BARE `python3` from PATH. That is a dependency inversion sitting inside a binary whose selling
// point is not needing Python: on a machine with no `python3`, the helper failed on every piped
// invocation, and its failure is a warning about output that may be truncated — i.e. the one
// mitigation for a silent-corruption bug, reduced to a line of noise, on exactly the machines this
// binary exists for.
//
// It now prefers THIS: the interpreter `uv sync` provisioned into the engine's own tools directory.
// It belongs to the engine, it is pinned by `uv.lock`, and it is present whenever the engine has been
// provisioned at all. Returns null when it has not been, and the caller falls back to what it did
// before — a machine mid-bootstrap is not a machine to refuse to dispatch on.
export function enginePython(engineRoot: string): string | null {
  const p = path.join(projectEnv(engineRoot), "bin", "python3");
  try {
    fs.accessSync(p, fs.constants.X_OK);
    return p;
  } catch {
    return null;
  }
}

export function loadPin(engineRoot: string): UvPin {
  const p = path.join(engineRoot, PIN_REL);
  let pin: UvPin;
  try {
    pin = JSON.parse(fs.readFileSync(p, "utf8")) as UvPin;
  } catch (e) {
    throw new ProvisionRefusal(`plainkeep: cannot read the uv pin at ${p} (${(e as Error).message})`);
  }
  if (!/^[0-9]+\.[0-9]+\.[0-9]+$/.test(pin?.version ?? "")) {
    throw new ProvisionRefusal(`plainkeep: the uv pin at ${p} names no usable version`);
  }
  const arts = pin.artifacts;
  if (!arts || typeof arts !== "object" || Object.keys(arts).length === 0) {
    throw new ProvisionRefusal(`plainkeep: the uv pin at ${p} lists no artifacts`);
  }
  for (const [target, digest] of Object.entries(arts)) {
    if (typeof digest !== "string" || !/^[0-9a-f]{64}$/.test(digest)) {
      throw new ProvisionRefusal(`plainkeep: the uv pin at ${p} has a malformed sha256 for ${target}`);
    }
  }
  return pin;
}

// `platform.system()`/`platform.machine()`'s answers, from node's spellings. The musl question the
// Python side answers with `platform.libc_ver()` is answered here by looking for the loader, because
// there is no libc probe in node: a glibc host has `/lib/ld-linux-*.so*` or `/lib64/`, a musl one has
// `/lib/ld-musl-*.so*`. Both are heuristics, both fail loudly (a uv that will not start), and the
// parity test asserts the two agree ON THIS HOST — which is the only host either can be checked on.
export function platformTarget(): string {
  const arch = { arm64: "aarch64", aarch64: "aarch64", x64: "x86_64", x86_64: "x86_64" }[os.arch()];
  if (!arch) throw new ProvisionRefusal(`plainkeep: no uv build is pinned for this CPU (${os.arch()})`);
  if (process.platform === "darwin") return `${arch}-apple-darwin`;
  if (process.platform === "linux") {
    const musl = fs.existsSync("/lib/ld-musl-x86_64.so.1") || fs.existsSync("/lib/ld-musl-aarch64.so.1");
    return `${arch}-unknown-linux-${musl ? "musl" : "gnu"}`;
  }
  throw new ProvisionRefusal(`plainkeep: no uv build is pinned for this platform (${process.platform})`);
}

export type Artifact = { target: string; url: string; sha256: string; member: string };

export function artifact(pin: UvPin, target?: string): Artifact {
  const t = target ?? platformTarget();
  const sha256 = pin.artifacts[t];
  if (!sha256) {
    const known = Object.keys(pin.artifacts).sort().join(", ");
    throw new ProvisionRefusal(
      `plainkeep: uv ${pin.version} is not pinned for ${t}\n  pinned targets: ${known}`,
    );
  }
  return {
    target: t,
    url: pin.url_template.replace("{version}", pin.version).replace("{target}", t),
    sha256,
    member: (pin.member_template ?? "uv-{target}/uv").replace("{target}", t),
  };
}

export function uvPath(engineRoot: string, pin: UvPin): string {
  return path.join(toolsDir(engineRoot), "uv", pin.version, "uv");
}

// Byte-for-byte what `provision.offline_refusal()` prints. Pinned by test/run_provision.py, which
// runs both and compares — an air-gapped operator must not get two different sets of instructions
// depending on which half of the engine happened to answer.
export function offlineRefusal(engineRoot: string, pin: UvPin): string {
  const a = artifact(pin);
  const dest = uvPath(engineRoot, pin);
  const tmp = `/tmp/uv-${a.target}.tar.gz`;
  return [
    `plainkeep needs uv ${pin.version} and cannot reach the network to fetch it.`,
    "",
    "Fetch it on a machine that can, verify it, and put it where the engine expects it:",
    "",
    `  curl -fsSL -o ${tmp} \\`,
    `    ${a.url}`,
    `  echo '${a.sha256}  ${tmp}' | shasum -a 256 -c -`,
    `  mkdir -p ${path.dirname(dest)}`,
    `  tar -xzOf ${tmp} ${a.member} > ${dest}`,
    `  chmod 555 ${dest}`,
    "",
    "Nothing was left half-installed; re-run the same command when it is in place.",
  ].join("\n");
}

function sha256File(p: string): string {
  return createHash("sha256").update(fs.readFileSync(p)).digest("hex");
}

function unseal(dir: string): void {
  // Best effort, and only ever a prelude to a removal — `_chmod_tree`'s rule.
  const walk = (d: string): void => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      try {
        fs.chmodSync(p, e.isDirectory() ? 0o755 : 0o644);
      } catch {
        /* the rmtree that follows fails loudly on its own */
      }
      if (e.isDirectory()) walk(p);
    }
  };
  try {
    fs.chmodSync(dir, 0o755);
    walk(dir);
  } catch {
    /* as above */
  }
}

// Extract ONE named member. `tar -xzO <member>` writes just that member to stdout, so the archive
// never names a destination — the same property `provision.py` gets from reading the member itself.
function extractMember(archive: string, member: string, dest: string): void {
  const r = spawnSync("tar", ["-xzOf", archive, member], { maxBuffer: 1 << 30 });
  if (r.status !== 0 || !r.stdout || r.stdout.length === 0) {
    throw new ProvisionRefusal(
      `plainkeep: cannot extract ${member} from the uv archive` +
        (r.stderr?.length ? ` (${r.stderr.toString().trim().slice(0, 200)})` : ""),
    );
  }
  fs.writeFileSync(dest, r.stdout);
}

export async function ensureUv(
  engineRoot: string,
  opts: { allowNetwork?: boolean; checkDigests?: boolean } = {},
): Promise<string> {
  const allowNetwork = opts.allowNetwork !== false;
  // THE GATE, before the pin is even read — the pin is the file being defended, and this function is
  // reachable on its own (`--core-provision ensure-uv`). `checkDigests: false` is for the one caller
  // that has already asked, so the tree is hashed once per provisioning run.
  if (opts.checkDigests !== false) requireDeliveredIntact(engineRoot);
  const pin = loadPin(engineRoot);
  const a = artifact(pin);
  const dest = uvPath(engineRoot, pin);
  if (fs.existsSync(dest)) {
    // Idempotent by CONTENT, not presence: a truncated download from a killed run is replaced, not
    // trusted for the life of the engine.
    if (sha256File(dest) === a.sha256) return dest;
    unseal(path.dirname(dest));
    fs.rmSync(dest, { force: true });
  }
  if (!allowNetwork) throw new ProvisionRefusal(offlineRefusal(engineRoot, pin));
  const staging = path.join(toolsDir(engineRoot), `.incoming-uv-${pin.version}.${process.pid}`);
  fs.mkdirSync(staging, { recursive: true });
  try {
    const archive = path.join(staging, `uv-${pin.version}.tar.gz`);
    let bytes: ArrayBuffer;
    try {
      const r = await fetch(a.url, { redirect: "follow" });
      if (!r.ok) {
        throw new ProvisionRefusal(
          `plainkeep: cannot download uv ${pin.version} (HTTP ${r.status} from ${a.url})\n` +
            offlineRefusal(engineRoot, pin),
        );
      }
      bytes = await r.arrayBuffer();
    } catch (e) {
      if (e instanceof ProvisionRefusal) throw e;
      throw new ProvisionRefusal(
        `plainkeep: cannot download uv ${pin.version} (${(e as Error).message})\n` +
          offlineRefusal(engineRoot, pin),
      );
    }
    fs.writeFileSync(archive, Buffer.from(bytes));
    const got = sha256File(archive);
    if (got !== a.sha256) {
      // THE POINT OF THE PIN. The `finally` below deletes the download; nothing is installed.
      throw new ProvisionRefusal(
        `plainkeep: the uv ${pin.version} download does not match its pinned sha256\n` +
          `  expected ${a.sha256}\n  got      ${got}\n  from     ${a.url}\n` +
          "  the download was deleted and nothing was installed",
      );
    }
    const binary = path.join(staging, "uv");
    extractMember(archive, a.member, binary);
    fs.rmSync(archive, { force: true });
    // Sealed before it is reachable under its final name — no window in which a writable,
    // unverified, executable binary sits where everything else will look for uv.
    fs.chmodSync(binary, 0o555);
    fs.mkdirSync(path.dirname(path.dirname(dest)), { recursive: true });
    if (fs.existsSync(path.dirname(dest))) {
      unseal(path.dirname(dest));
      fs.rmSync(path.dirname(dest), { recursive: true, force: true });
    }
    fs.renameSync(staging, path.dirname(dest));
    fs.chmodSync(path.dirname(dest), 0o555);
    return dest;
  } finally {
    // NO PARTIAL PROVISIONING IS LEFT BEHIND. Every failure path lands here, the checksum refusal
    // included; on the success path the staging directory has already been renamed away.
    if (fs.existsSync(staging)) fs.rmSync(staging, { recursive: true, force: true });
  }
}

// `uv sync --frozen` — see `provision.sync_argv`/`sync_env` for why each flag and each variable is
// there. Duplicated here rather than shelled out to Python, because the machine this runs on may not
// have a Python to shell out to; that is the whole reason this file exists.
export function syncArgv(engineRoot: string, uv: string, extras: string[]): string[] {
  const argv = [uv, "sync", "--frozen", "--no-config", "--project", engineRoot];
  for (const e of extras) argv.push("--extra", e);
  return argv;
}

// `uv lock --check` — run BEFORE the sync, because `--frozen` does not check the lock against the
// project (measured; see `provision.sync_argv`) and the two flags that would are mutually exclusive
// on one command line.
export function checkArgv(engineRoot: string, uv: string): string[] {
  return [uv, "lock", "--check", "--no-config", "--project", engineRoot];
}

// THE CHECKSUM GATE, ported (`enginetree.digest_problems`), and this is not a convenience: on a
// machine with no system python3 THIS is the provisioning path, so a version of it that skipped the
// gate would mean "a tampered lock fails its checksum rather than installing" held only on machines
// that did not need this file.
//
// EVERY RECORDED FILE, not a named pair. This used to hard-code `["pyproject.toml", "uv.lock"]`, and
// the file it left out was `bin/lib/uvpin.json` — the one that chooses which binary is downloaded and
// what digest it is checked against. See `provision.require_delivered_intact` for the measurement;
// the short version is that the narrow form let an attacker-supplied uv be installed, sealed and run
// on this path too. ~114 sha256 of small files, ahead of a 35 MB download.
//
// Scoped to an INSTALLED tree (`<…>/engine/<version>/`), matching `enginetree._looks_installed`: a
// contributor's checkout has no manifest and is not claiming to.
export function deliveredDigestProblems(engineRoot: string): string[] {
  const version = path.basename(engineRoot);
  if (path.basename(path.dirname(engineRoot)) !== "engine" || version.startsWith(".")) return [];
  const manifest = path.join(path.dirname(engineRoot), ".digests", `${version}.json`);
  let files: Record<string, string>;
  try {
    files = (JSON.parse(fs.readFileSync(manifest, "utf8")) as { files: Record<string, string> }).files;
  } catch {
    return [`no recorded checksums for this engine (${manifest} is missing or unreadable)`];
  }
  if (!files || typeof files !== "object" || Object.keys(files).length === 0) {
    return [`no recorded checksums for this engine (${manifest} records no files)`];
  }
  const problems: string[] = [];
  for (const rel of Object.keys(files).sort()) {
    try {
      if (sha256File(path.join(engineRoot, rel)) !== files[rel]) {
        problems.push(`${rel} does not match its recorded checksum`);
      }
    } catch {
      problems.push(`${rel} is recorded but missing`);
    }
  }
  // AND THE OTHER DIRECTION: a file that is PRESENT but was never recorded. Python's
  // `digest_problems` has always reported these when it checks the whole tree, and this port did
  // not — invisible while the gate named two files by hand (neither can be "extra"), and a live
  // parity hole the moment the gate widened to the tree. Checking only recorded paths answers
  // "was anything CHANGED"; an attacker who ADDS `bin/lib/sitecustomize.py` changes nothing. The
  // core is the only provisioning path on a machine with no system python3, so a check that holds
  // there and not here holds exactly where it is not needed.
  for (const rel of ownedPaths(engineRoot)) {
    if (!(rel in files)) problems.push(`${rel} is present but was never recorded`);
  }
  return problems.sort();
}

// `enginetree._owned_paths`, ported: every owned FILE as sorted relative posix paths. Symlinks are
// listed by neither side (`frontends/raycast` may legitimately carry one) and `__pycache__` is
// excluded, both matching the Python walk exactly — a path one implementation calls owned and the
// other does not is a disagreement about what "intact" means.
function ownedPaths(engineRoot: string): string[] {
  const rels: string[] = [];
  for (const rel of OWNED_FILES) {
    try {
      if (fs.statSync(path.join(engineRoot, rel)).isFile()) rels.push(rel);
    } catch {
      /* absent is the manifest's business, checked above */
    }
  }
  const walk = (dir: string, prefix: string): void => {
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      if (e.name === "__pycache__") continue;
      const rel = `${prefix}/${e.name}`;
      if (e.isSymbolicLink()) continue;
      if (e.isDirectory()) walk(path.join(dir, e.name), rel);
      else if (e.isFile()) rels.push(rel);
    }
  };
  for (const tree of OWNED_TREES) walk(path.join(engineRoot, ...tree.split("/")), tree);
  return [...new Set(rels)].sort();
}

// The gate as a REFUSAL rather than a list, so every entry point spells it the same way and none of
// them can forget to look at the answer. `--core-provision ensure-uv` is reachable without `sync`,
// and it is the command that installs and seals an executable.
export function requireDeliveredIntact(engineRoot: string): void {
  const problems = deliveredDigestProblems(engineRoot);
  if (problems.length) {
    throw new TamperRefusal(
      "plainkeep: refusing to provision from a delivered project that does not match its " +
        "recorded checksums:\n  " + problems.join("\n  ") +
        "\n  the engine tree was modified after it was installed — reinstall it",
    );
  }
}

export function syncEnv(engineRoot: string, offline: boolean): Record<string, string> {
  const env: Record<string, string> = {
    ...(process.env as Record<string, string>),
    UV_NO_CONFIG: "1",
    UV_PROJECT_ENVIRONMENT: projectEnv(engineRoot),
    UV_PYTHON_INSTALL_DIR: path.join(toolsDir(engineRoot), PYTHON_DIRNAME),
  };
  if (offline) env.UV_OFFLINE = "1";
  return env;
}
