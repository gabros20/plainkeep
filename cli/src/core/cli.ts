// The core binary's argv handler. The identity probes (`--version`, `--core-selftest`) and the
// hidden `--core-*` introspection flags short-circuit here; EVERY other argv is a verb dispatch
// (./dispatch.ts) — gate, resolve, one spawn — reproducing the bash floor exactly.
import { CORE_VERSION } from "./version.js";
import { dispatch } from "./dispatch.js";
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

export interface CoreResult {
  stdout?: string;
  stderr?: string;
  code: number;
  // Set ONLY when a dispatched verb was killed by a signal. main.ts re-raises it on this process so
  // the wait status a caller sees is a signal death, exactly like the bash floor's `exec`'d child —
  // `code` (128+N) is then just the fallback for a signal that does not kill us. See dispatch.ts.
  signal?: NodeJS.Signals;
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
  return { stderr: `plainkeep-core: unknown --core-api spec: ${spec}`, code: 2 };
}

// The identity line both probes emit. It unambiguously names the core binary and carries a version,
// so later differential/acceptance tests can tell this artifact apart from the legacy plainkeep-ui.
export const CORE_IDENTITY = `plainkeep-core ${CORE_VERSION}`;

export function runCore(argv: string[]): CoreResult {
  if (argv.length === 1 && (argv[0] === "--version" || argv[0] === "-v")) {
    return { stdout: CORE_IDENTITY, code: 0 };
  }
  if (argv.length === 1 && argv[0] === "--core-selftest") {
    return { stdout: `${CORE_IDENTITY} selftest ok`, code: 0 };
  }
  // Test-only introspection flags (hidden, no help text) consumed by test/run_core_parity.py. They
  // expose the ported resolver so a Python-owned differential harness can prove TS ≡ Python.
  if (argv[0] === "--core-resolve") {
    // Mirrors resolver.py __main__: print the resolved run.py path (exit 0) or nothing (exit 4).
    const p = runPy(argv[1] ?? "");
    return p ? { stdout: p, code: 0 } : { code: 4 };
  }
  if (argv[0] === "--core-api") {
    return coreApi(argv[1] ?? "");
  }
  // Hidden gate probe (no help text) consumed by test/run_core_parity.py's "gate" comparator: runs
  // the ported main_cli semantics (known-verb check + did-you-mean, risk gate, audit log, stderr)
  // and exits with the gate code, spawning NOTHING — dispatch() below runs the same gate and then
  // goes on to spawn the verb.
  if (argv[0] === "--core-gate") {
    return mainCli(argv.slice(1));
  }
  // Everything else is a verb (including no argv at all, which is the default verb `help`).
  return dispatch(argv);
}
