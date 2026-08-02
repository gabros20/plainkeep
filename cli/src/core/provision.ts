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

export type UvPin = {
  version: string;
  url_template: string;
  member_template?: string;
  artifacts: Record<string, string>;
};

export class ProvisionRefusal extends Error {}

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
  opts: { allowNetwork?: boolean } = {},
): Promise<string> {
  const allowNetwork = opts.allowNetwork !== false;
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

// THE CHECKSUM GATE, ported (`enginetree.digest_problems(root, only=…)`), and this is not a
// convenience: on a machine with no system python3 THIS is the provisioning path, so a version of it
// that skipped the gate would mean "a tampered lock fails its checksum rather than installing" held
// only on machines that did not need this file. The narrow form — the two files about to be handed to
// uv — for the same reason the Python side passes `only`: two digests instead of ~114.
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
  const problems: string[] = [];
  for (const rel of ["pyproject.toml", "uv.lock"]) {
    const want = files?.[rel];
    if (!want) {
      problems.push(`${rel} has no recorded checksum`);
      continue;
    }
    try {
      if (sha256File(path.join(engineRoot, rel)) !== want) {
        problems.push(`${rel} does not match its recorded checksum`);
      }
    } catch {
      problems.push(`${rel} is recorded but missing`);
    }
  }
  return problems;
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
