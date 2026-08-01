// vaultroot.ts — WHICH vault this invocation acts on, in the compiled core (ADR-014, Phase 2 Task 1b).
//
// The decision itself is NOT ported. It runs `python3 <engine>/bin/lib/vaultroot.py --select`, the
// exact command the bash floor runs, and this module only carries the answer (or the refusal) into
// the dispatcher. That is a deliberate architectural choice and it is the same one guardrail.ts
// already made for `classify()`: the dispatcher MECHANICS are ported and proven byte-equal by the
// parity oracle, the SAFETY MODEL is not duplicated at all.
//
// Why, stated as the trade rather than as a preference:
//   * Byte-identical refusals across the two dispatchers become a property of the code rather than
//     of a differential that has to be maintained. Discovery refuses in roughly fifteen distinct
//     ways (malformed marker, unknown schema, duplicate registry entry, a `default` naming nothing,
//     a vault registered at another path, a policy-denied location, …), each with its own message
//     and hint. A hand-ported registry validator whose text must stay byte-equal to Python's is the
//     drift this repo has already paid for once.
//   * It COSTS ONE PROCESS PER INVOCATION — measured at ~23 ms on bun 1.3.14 / macOS arm64 / CPython
//     3.12, against a ~24 ms core dispatch. That dents ADR-013's headline (ONE spawn per verb where
//     the floor pays three; it is now two vs four). The O_NONBLOCK helper took the same shape of
//     trade for a weaker reason. If that cost ever matters more than single-sourcing the safety
//     model, the way out is a real port behind a NEW parity catalog that fixtures vaults, registries
//     and cwds — not an unproven reimplementation.
//
// The ENGINE location is derived from the executable, and that is CORRECT — note the distinction
// ADR-014 draws and Task 1b turns into a rule. Deriving the ENGINE from where the code lives is
// right (the binary ships inside the engine tree, at <engine>/.local/bin/plainkeep-core, exactly as
// resolver.py's `ENGINE_BIN = parents[1]  # ships with the CODE` always did). Deriving the VAULT
// from it is the assumption being deleted, and nothing here does that.
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

export const EXIT_USAGE = 2;

// A refusal that must reach the shell with a code from the frozen protocol. It is a distinct class
// because main.ts's last-resort catch maps everything else to deny (5): "no vault selected" is a
// USAGE error (2), and collapsing it into 5 would tell an operator the guardrail denied them when
// what actually happened is that they never picked a vault.
export class VaultRefusal extends Error {
  readonly code: number;
  constructor(message: string, code: number = EXIT_USAGE) {
    super(message);
    this.name = "VaultRefusal";
    this.code = code;
  }
}

// PLAINKEEP_HOME, with NO fallback. Every consumer in the binary (resolver.ts's opsHome(),
// guardrail.ts's plainkeepHome(), dispatch.ts) reads the root through here, so there is exactly one
// place that could ever grow one back. dispatch() sets the env from the validated discovery result
// before anything reads it, so in a real invocation this always takes the value branch.
export function requireHome(): string {
  const env = process.env.PLAINKEEP_HOME;
  if (env) return env;
  throw new VaultRefusal(
    "plainkeep: no vault selected — PLAINKEEP_HOME is unset (plainkeep no longer guesses one from " +
      "where the binary is installed)",
  );
}

// The engine tree this code belongs to. Two CODE-relative candidates, in order, and the first one
// that actually carries the discovery module wins:
//
//   1. two parents above the executable — `<engine>/.local/bin/plainkeep-core` → `<engine>`. This is
//      the compiled binary, i.e. every real invocation.
//   2. three parents above this module — `<engine>/cli/src/core` → `<engine>`. This is `bun test`
//      and `bun run src/core/main.ts`, where `process.execPath` is bun itself and candidate 1 points
//      at whoever installed bun.
//
// Both are CODE-relative, which is the distinction ADR-014 turns into a rule: deriving the ENGINE
// from where the code lives is correct (it is the same thing resolver.py's `ENGINE_BIN` has always
// done), deriving the VAULT from it is the assumption Task 1b deletes. Nothing below reads a data
// root, and the probe is for a file that ships with the engine — never for a vault.
const DISCOVERY_REL = ["bin", "lib", "vaultroot.py"] as const;

export function engineRoot(): string {
  const candidates = [
    path.resolve(path.dirname(process.execPath), "..", ".."),
    path.resolve(import.meta.dir, "..", "..", ".."),
  ];
  for (const c of candidates) {
    if (fs.existsSync(path.join(c, ...DISCOVERY_REL))) return c;
  }
  throw new VaultRefusal(
    "plainkeep: cannot locate the plainkeep engine — no bin/lib/vaultroot.py under " +
      candidates.join(" or "),
  );
}

// The global `--vault` selector, PRE-VERB ONLY, removed from argv before anything else looks at it.
// `plainkeep --vault work capture x` selects; `plainkeep capture --vault work` does NOT — there it
// is capture's own argument. One unambiguous position, so no verb can be surprised by an argument
// that also steers the safety model, and so completion, interception and the child's argv never have
// to know the selector exists.
export function takeVaultSelector(argv: string[]): { selector: string | null; rest: string[] } {
  if (argv[0] !== "--vault") return { selector: null, rest: argv };
  if (argv.length < 2) {
    throw new VaultRefusal("plainkeep: --vault needs a value (<name|id|absolute-path>)");
  }
  return { selector: argv[1], rest: argv.slice(2) };
}

export interface Root {
  root: string;
  id: string;
}

// Run the shared discovery module. stdout is two lines — canonical root, then vault id. A refusal
// keeps ITS exit code (2 usage / 5 policy-denied) and ITS stderr, which is written through verbatim
// so the floor and the core are indistinguishable to a caller.
export function discoverRoot(selector: string | null): Root {
  const script = path.join(engineRoot(), ...DISCOVERY_REL);
  const args = selector === null ? [script, "--select"] : [script, "--select", "--vault", selector];
  // Bare `python3`, matching the floor: the venv interpreter lives under the root being resolved,
  // and vaultroot.py is stdlib-only. stderr is INHERITED so a refusal's text reaches the terminal
  // exactly as Python wrote it (no re-encoding, no truncation, no line the core added).
  // `env` is passed EXPLICITLY rather than inherited by omission. Under bun, an inherited env is a
  // snapshot that does not see later `process.env.X = …` assignments, which is exactly how the tests
  // (and any in-process caller) set the root — measured: the child read PLAINKEEP_HOME as unset
  // while the parent had just assigned it.
  const r = spawnSync("python3", args, {
    stdio: ["ignore", "pipe", "inherit"],
    encoding: "utf-8",
    env: { ...process.env },
  });
  if (r.error || r.status === null || r.status === undefined) {
    const why = (r.error as { code?: string } | undefined)?.code ?? "unknown error";
    throw new VaultRefusal(
      `plainkeep: could not run vault discovery (${why}) — expected ${script}`,
    );
  }
  if (r.status !== 0) {
    // The message already went to the real stderr via stdio "inherit"; carry only the code, with an
    // EMPTY message so main.ts prints nothing a second time.
    throw new VaultRefusal("", r.status);
  }
  const lines = (r.stdout ?? "").split("\n");
  const root = lines[0] ?? "";
  const id = lines[1] ?? "";
  if (!root || !id) {
    throw new VaultRefusal("plainkeep: vault discovery returned no root — the engine tree is broken");
  }
  return { root, id };
}
