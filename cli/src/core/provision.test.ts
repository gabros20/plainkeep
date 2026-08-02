// Provisioning, from the compiled core's side (Phase 2 Task 4a).
//
// The Python suite (test/run_provision.py) owns the cross-implementation comparison — it runs BOTH
// this binary and `bin/lib/provision.py` against the same installed engine and asserts they agree
// byte for byte. What is here is what only a bun test can reach: the pure functions, and
// `blockingRestoreInterpreter`, which is ADR-013's carried inversion and has no CLI surface at all.
import { describe, expect, test } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, chmodSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import * as path from "node:path";
import { blockingRestoreInterpreter } from "./dispatch.js";
import {
  artifact,
  enginePython,
  loadPin,
  offlineRefusal,
  platformTarget,
  projectEnv,
  ProvisionRefusal,
  checkArgv,
  syncArgv,
  syncEnv,
  uvPath,
} from "./provision.js";

const REPO = path.resolve(import.meta.dir, "..", "..", "..");

function fakeEngine(): string {
  const root = mkdtempSync(path.join(tmpdir(), "pk-provision-ts-"));
  mkdirSync(path.join(root, "bin", "lib"), { recursive: true });
  writeFileSync(
    path.join(root, "bin", "lib", "uvpin.json"),
    JSON.stringify({
      version: "9.9.9",
      url_template: "https://example.invalid/{version}/uv-{target}.tar.gz",
      member_template: "uv-{target}/uv",
      artifacts: { [platformTarget()]: "a".repeat(64) },
    }),
  );
  return root;
}

describe("the pin", () => {
  test("the repository's own pin parses and covers this host", () => {
    const pin = loadPin(REPO);
    expect(pin.version).toMatch(/^\d+\.\d+\.\d+$/);
    expect(artifact(pin).sha256).toMatch(/^[0-9a-f]{64}$/);
  });

  test("a malformed digest in our OWN pin refuses as a pin error, not later as a phantom tamper", () => {
    const root = fakeEngine();
    writeFileSync(
      path.join(root, "bin", "lib", "uvpin.json"),
      JSON.stringify({ version: "1.2.3", url_template: "x", artifacts: { a: "nope" } }),
    );
    expect(() => loadPin(root)).toThrow(ProvisionRefusal);
    rmSync(root, { recursive: true, force: true });
  });

  test("an unpinned target refuses by name and lists the targets that ARE pinned", () => {
    const pin = loadPin(REPO);
    expect(() => artifact(pin, "sparc64-unknown-linux-gnu")).toThrow(/not pinned for sparc64/);
    expect(() => artifact(pin, "sparc64-unknown-linux-gnu")).toThrow(/pinned targets:/);
  });
});

describe("where things land", () => {
  test("uv goes INSIDE the versioned engine tree, so it rolls back with the engine", () => {
    const root = fakeEngine();
    expect(uvPath(root, loadPin(root))).toBe(path.join(root, "tools", "uv", "9.9.9", "uv"));
    rmSync(root, { recursive: true, force: true });
  });

  test("the environment and any managed interpreter stay inside tools/, never at <project>/.venv", () => {
    const root = fakeEngine();
    const env = syncEnv(root, true);
    expect(env.UV_PROJECT_ENVIRONMENT).toBe(path.join(root, "tools", "venv"));
    expect(env.UV_PYTHON_INSTALL_DIR).toBe(path.join(root, "tools", "python"));
    // The operator's own uv config must not steer a resolution the engine pinned.
    expect(env.UV_NO_CONFIG).toBe("1");
    expect(env.UV_OFFLINE).toBe("1");
    rmSync(root, { recursive: true, force: true });
  });

  test("the offline refusal is a runnable two-step naming url, digest and destination", () => {
    const root = fakeEngine();
    const pin = loadPin(root);
    const text = offlineRefusal(root, pin);
    expect(text).toContain(artifact(pin).url);
    expect(text).toContain(artifact(pin).sha256);
    expect(text).toContain(uvPath(root, pin));
    expect(text).toContain("shasum -a 256 -c -");
    rmSync(root, { recursive: true, force: true });
  });

  test("sync uses --frozen; the lock/project agreement is a SEPARATE check (they are mutually exclusive)", () => {
    const root = fakeEngine();
    expect(syncArgv(root, "/uv", ["search"])).toEqual([
      "/uv", "sync", "--frozen", "--no-config", "--project", root, "--extra", "search",
    ]);
    expect(checkArgv(root, "/uv")).toEqual([
      "/uv", "lock", "--check", "--no-config", "--project", root,
    ]);
    rmSync(root, { recursive: true, force: true });
  });
});

describe("the pinned engine interpreter (ADR-013's carried O_NONBLOCK inversion)", () => {
  test("an unprovisioned engine has none — it is not guessed", () => {
    const root = fakeEngine();
    expect(enginePython(root)).toBeNull();
    rmSync(root, { recursive: true, force: true });
  });

  test("a provisioned engine's interpreter is found, and it is inside the engine", () => {
    const root = fakeEngine();
    const bin = path.join(root, "tools", "venv", "bin");
    mkdirSync(bin, { recursive: true });
    writeFileSync(path.join(bin, "python3"), "#!/bin/sh\nexec true\n");
    chmodSync(path.join(bin, "python3"), 0o755);
    expect(enginePython(root)).toBe(path.join(bin, "python3"));
    rmSync(root, { recursive: true, force: true });
  });

  // THE FIX ITSELF. The helper that clears O_NONBLOCK used to spawn whatever pickPython() answered,
  // whose floor is a BARE `python3` from PATH — inside a binary whose point is not needing one. It
  // now prefers the engine's own provisioned interpreter. Run from the checkout, engineRoot() is the
  // repository, so what this asserts depends on whether the checkout has been provisioned; BOTH
  // branches are a real assertion, and neither is "it returned something".
  test("blockingRestoreInterpreter prefers the engine's interpreter over the caller's bare python3", () => {
    const engineOwn = enginePython(REPO);
    const chosen = blockingRestoreInterpreter("python3");
    if (engineOwn) {
      expect(chosen).toBe(engineOwn);
      expect(chosen).not.toBe("python3");
      expect(chosen.startsWith(REPO)).toBe(true);
    } else {
      // Unprovisioned: the fallback is kept deliberately — between --install and the first
      // `plainkeep setup` there is a real window, and refusing to dispatch through it would be a
      // worse answer than behaving exactly as before.
      expect(chosen).toBe("python3");
    }
  });

  test("it never throws, even when the engine cannot be located — a mitigation must not become the failure", () => {
    expect(() => blockingRestoreInterpreter("/some/python3")).not.toThrow();
  });
});

describe("what is deliberately absent", () => {
  test("nothing in this module reads PATH — a system uv is ignored, not preferred", async () => {
    const src = await Bun.file(path.join(import.meta.dir, "provision.ts")).text();
    // `which`/PATH lookups are the mechanism by which a pin gets silently un-pinned. The one
    // permitted PATH-resolved spawn is `tar`, which extracts an already-verified archive.
    expect(src).not.toContain("process.env.PATH");
    expect(src).not.toContain('which("uv")');
  });

  test("the project itself declares no runtime dependencies — the stdlib floor is a contract", async () => {
    const toml = await Bun.file(path.join(REPO, "pyproject.toml")).text();
    expect(toml).toContain("dependencies = []");
  });
});
