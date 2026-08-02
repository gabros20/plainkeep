// The core binary's argv handler. The identity probes (`--version`, `--core-selftest`) and the
// hidden `--core-*` introspection flags short-circuit here; EVERY other argv is a verb dispatch
// (./dispatch.ts) — gate, resolve, one spawn — reproducing the bash floor exactly.
import { CORE_VERSION } from "./version.js";
import { dispatch, INTERCEPTS, interceptionFor } from "./dispatch.js";
import {
  iterCmds,
  knownVerbs,
  pluginNames,
  resolve,
  runPy,
  shadowed,
  sourceOf,
} from "./resolver.js";
import { mainCli } from "./guardrail.js";
import { takeVaultSelector, VaultRefusal } from "./vaultroot.js";

export interface CoreResult {
  stdout?: string;
  stderr?: string;
  code: number;
  // Set ONLY when a dispatched verb was killed by a signal, and always as a NUMBER — bun's signal
  // NAMES are wrong on macOS for the numbers where Linux and macOS disagree, so the name never
  // leaves dispatch.ts (see signalNumberOf). main.ts re-raises this number on the process so the
  // wait status a caller sees is a signal death, exactly like the bash floor's `exec`'d child;
  // `code` (128+N) is then the fallback for a signal that does not kill us.
  signal?: number;
}

// Compact JSON, no inter-token spaces — matches the parity probe's
// json.dumps(x, separators=(",", ":"), ensure_ascii=False), so ordered/set-valued API output is
// byte-identical to the Python side.
function coreApi(spec: string): CoreResult {
  if (spec === "known_verbs") {
    return { stdout: JSON.stringify([...knownVerbs()].sort()), code: 0 };
  }
  if (spec === "iter_cmds") {
    return { stdout: JSON.stringify(iterCmds()), code: 0 };
  }
  if (spec === "shadowed") {
    return { stdout: JSON.stringify(shadowed()), code: 0 };
  }
  if (spec === "plugin_names") {
    return { stdout: JSON.stringify(pluginNames()), code: 0 };
  }
  if (spec.startsWith("source_of:")) {
    return { stdout: JSON.stringify(sourceOf(spec.slice("source_of:".length))), code: 0 };
  }
  if (spec.startsWith("resolve:")) {
    const r = resolve(spec.slice("resolve:".length));
    return { stdout: JSON.stringify(r ? [r[0], r[1]] : null), code: 0 };
  }
  // What this binary answers itself, published so the parity harness never has to mirror it. Two
  // DIFFERENT things, deliberately kept apart rather than merged into one list:
  //   flags — argv shapes short-circuited BEFORE dispatch. Not comparable against the bash floor at
  //           all (the floor has no notion of `--version`, so it gates it as an unknown verb); the
  //           harness refuses to author a dispatch case for them.
  //   verbs — verbs answered in-process AFTER the gate (dispatch.ts INTERCEPTS), in TWO buckets,
  //           because "an interception is byte-comparable with the Python verb" is true of
  //           `__complete` and will be false of what Tasks 6–7 register:
  //             comparable    — the whole invocation can be run through both dispatchers and
  //                             byte-compared. Byte-parity is what makes intercepting these
  //                             legitimate, so the harness must NOT skip them.
  //             noncomparable — answered in-process but not comparable AS AN INVOCATION: a TUI's
  //                             output is non-deterministic (it paints a terminal), an MCP server
  //                             is a session rather than a command. Their behavior belongs in a bun
  //                             test or a PTY/protocol harness, not the dispatcher differential.
  //           The bucket is declared at each registration (Interception.comparable), so a new
  //           interception cannot be added without classifying it, and the harness reads that
  //           classification instead of carrying a skip-list that goes stale.
  if (spec === "intercepts") {
    // Object.keys is own-keys-only, so these are the real registrations (the prototype hazard that
    // interceptionFor() guards lives in the LOOKUP, not here) — and each key is then read back
    // through that same accessor rather than indexed directly, so this file carries no second
    // spelling of the rule.
    const verbs = Object.keys(INTERCEPTS).sort();
    const bucket = (want: boolean) => verbs.filter((v) => interceptionFor(v)?.comparable === want);
    return {
      stdout: JSON.stringify({
        flags: { always: [...INTERCEPTED_FLAGS_ALWAYS], bare: [...INTERCEPTED_FLAGS_BARE] },
        verbs: { comparable: bucket(true), noncomparable: bucket(false) },
      }),
      code: 0,
    };
  }
  return { stderr: `plainkeep-core: unknown --core-api spec: ${spec}`, code: 2 };
}

// The identity line both probes emit. It unambiguously names the core binary and carries a version,
// so later differential/acceptance tests can tell this artifact apart from the legacy plainkeep-ui.
export const CORE_IDENTITY = `plainkeep-core ${CORE_VERSION}`;

// The argv shapes runCore() answers ITSELF, before any of it reaches dispatch(). These two arrays
// are not documentation of the branches below — they DRIVE them, and `--core-api intercepts`
// publishes them, so the parity harness can ask the binary which shapes are not comparable across
// modes instead of restating these rules in Python and going stale the first time a task adds one.
//
// BARE intercepts only as the whole argv (`--version extra` is a verb, and both dispatchers refuse
// it identically); ALWAYS intercepts on argv[0] whatever follows.
export const INTERCEPTED_FLAGS_BARE = ["--version", "-v", "--core-selftest"] as const;
export const INTERCEPTED_FLAGS_ALWAYS = ["--core-resolve", "--core-api", "--core-gate"] as const;

export function runCore(rawArgv: string[]): CoreResult | Promise<CoreResult> {
  // The global `--vault` selector comes off FIRST, before the identity probes, before the hidden
  // introspection flags and before dispatch() ever sees an argv. Pre-verb only, and gone by the time
  // anything downstream — the gate, completion, the TUI/MCP interceptions, the child's argv — could
  // mistake it for something of its own. `plainkeep --vault work --version` is still the version
  // probe; `plainkeep capture --vault work` is still capture's own argument.
  const { selector, rest: argv } = takeVaultSelector(rawArgv);
  const head = argv[0] ?? "";
  if (argv.length === 1 && (INTERCEPTED_FLAGS_BARE as readonly string[]).includes(head)) {
    if (head === "--core-selftest") return { stdout: `${CORE_IDENTITY} selftest ok`, code: 0 };
    return { stdout: CORE_IDENTITY, code: 0 };
  }
  // Test-only introspection flags (hidden, no help text) consumed by test/run_core_parity.py. They
  // expose the ported resolver so a Python-owned differential harness can prove TS ≡ Python.
  if ((INTERCEPTED_FLAGS_ALWAYS as readonly string[]).includes(head)) {
    // None of these three honour a vault SELECTION — they answer for whatever PLAINKEEP_HOME names,
    // which is what their only caller (the parity harness) always sets. So a selector that reaches
    // here is REFUSED rather than dropped: silently ignoring the one flag whose entire purpose is to
    // steer which vault gets read and written is the wrong failure mode, and it is worse than an
    // unimplemented one because the caller cannot tell the difference from a green exit.
    if (selector !== null) {
      throw new VaultRefusal(
        `plainkeep: --vault is not honoured by ${head} — these probes answer for PLAINKEEP_HOME ` +
          `only. Set PLAINKEEP_HOME to the vault you mean.`,
      );
    }
    if (head === "--core-resolve") {
      // Mirrors resolver.py __main__: print the resolved run.py path (exit 0) or nothing (exit 4).
      const p = runPy(argv[1] ?? "");
      return p ? { stdout: p, code: 0 } : { code: 4 };
    }
    if (head === "--core-api") return coreApi(argv[1] ?? "");
    // Hidden gate probe: runs the ported main_cli semantics (known-verb check + did-you-mean, risk
    // gate, audit log, stderr) and exits with the gate code, spawning NOTHING — dispatch() below runs
    // the same gate and then goes on to spawn the verb.
    return mainCli(argv.slice(1));
  }
  // Everything else is a verb (including no argv at all, which is the default verb `help`).
  return dispatch(argv, selector);
}
