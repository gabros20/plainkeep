// dispatch.ts — the dispatcher contract, in-process. A faithful port of the root `plainkeep` bash
// dispatcher (now the shim's "bash floor", preserved verbatim there): resolve PLAINKEEP_HOME →
// pick the interpreter → gate the verb → resolve its run.py → ONE spawn, with the child's exit
// status (including a signal death) passed through.
//
// The whole point of the port is that the floor pays THREE process spawns per verb (guardrail.py,
// resolver.py, run.py) where this pays exactly ONE: the gate and the resolution happen in-process
// via guardrail.ts / resolver.ts, which the parity oracle proves byte-equivalent to their Python
// originals. Everything observable — exit code, stdout, stderr, the audit log line, the child's
// argv and env — must stay identical to the floor; test/cases/core-parity/dispatcher.json runs the
// same invocations through both and compares.
//
// Line references below are to the bash floor (the `plainkeep` script's `pk_floor` function).
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import type { CoreResult } from "./cli.js";
import { completeIntercept } from "./complete.js";
import { EXIT_NOT_FOUND, EXIT_OK, mainCli } from "./guardrail.js";
import { runPy } from "./resolver.js";

// Exit codes for a spawn that never became a child. OFF the frozen gate protocol (0/2/3/4/5) by
// design — these are the shell's own conventions, and the floor reaches them through bash's `exec`
// failure path rather than through any plainkeep decision: 127 = not found, 126 = found but not
// executable. See spawnVerb() for the disclosed text divergence.
const EXIT_NOT_EXECUTABLE = 126;
const EXIT_COMMAND_NOT_FOUND = 127;

// "The dispatcher could not start the verb, for a reason that is not the interpreter being missing
// or unexecutable" — EAGAIN, EMFILE, ENOMEM and friends. It deliberately is NOT 127: that code is
// the shell's "command not found", i.e. a diagnosis, and claiming it for a process-table limit sends
// the operator to look for a file that is sitting right there. Chosen from the same clear band as
// EXIT_UNKNOWN_SIGNAL below and for the same reasons; the two are adjacent so they read as a pair.
const EXIT_SPAWN_FAILED = 201;

// PLAINKEEP_HOME, resolved EXACTLY as the floor's `PK="${PLAINKEEP_HOME:-...}"`: a caller-supplied
// value is trusted VERBATIM — never canonicalized, never rewritten. It is the vault's identity (the
// resolver's engine bin/, the guardrail's .logs, every verb's own path derivation all hang off it),
// and the shim contract is to preserve what the caller supplied.
//
// The default, when nothing is supplied, is two parents above the executable
// (`<home>/.local/bin/plainkeep-core` → `<home>`), mirroring resolver.ts's opsHome() and
// guardrail.ts's plainkeepHome(). Note that this default is PHYSICAL where the floor's is LOGICAL:
// Bun hands back an already-realpath'd process.execPath (a compiled binary has no logical argv[0] to
// recover — argv is ["bun", "/$bunfs/root/main"]), while bash's `cd "$(dirname "$0")" && pwd` prints
// the logical path. On macOS that is /private/tmp/v vs /tmp/v for a vault reached through a symlink.
// It only bites when the binary is invoked DIRECTLY with no PLAINKEEP_HOME: through the shim (the
// only supported entrypoint) PLAINKEEP_HOME is always exported first, so the env branch decides and
// both sides agree byte-for-byte. Recorded in .orchestrate/task-4-report.md.
export function resolveHome(): string {
  const env = process.env.PLAINKEEP_HOME;
  if (env) return env;
  return path.resolve(path.dirname(process.execPath), "..", "..");
}

// The verb the floor would dispatch. Two bash behaviors, both load-bearing:
//   * `VERB="${1:-help}"` — `:-` substitutes on unset OR EMPTY, so `plainkeep ""` dispatches `help`
//     and the empty string is consumed by the following `shift`, not passed on as an arg.
//   * `case "$VERB" in -h|--help) VERB=help;; esac` — applied AFTER the shift, so the remaining args
//     survive: `plainkeep --help foo` is `help foo`.
export function verbFromArgv(argv: string[]): { verb: string; args: string[] } {
  const first = argv.length ? argv[0] : "";
  const verb = first === "" ? "help" : first;
  const args = argv.slice(1);
  return { verb: verb === "-h" || verb === "--help" ? "help" : verb, args };
}

function isExecutable(p: string): boolean {
  try {
    fs.accessSync(p, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

// Interpreter selection (ADR-008 / floor lines 20-23), including the part that is easy to drop: the
// venv python must not only EXIST and be executable (`-x`), it must actually START. A
// `.venv/bin/python3` symlink survives a system-python upgrade or an ABI break while failing to
// exec, and picking it blindly would return 126/127 on EVERY verb. So `-c ''` is run and its exit
// status checked — one probe, and ONLY when the venv python is present, so the no-venv path (the
// zero-install floor) pays nothing.
//
// stdio mirrors the floor's `"$PK/.venv/bin/python3" -c '' 2>/dev/null`: stderr discarded, stdout
// inherited (a probe that somehow prints is passed through identically), stdin closed.
export function pickPython(home: string): string {
  const venv = path.join(home, ".venv", "bin", "python3");
  if (!isExecutable(venv)) return "python3";
  const probe = spawnSync(venv, ["-c", ""], { stdio: ["ignore", "inherit", "ignore"] });
  return probe.status === 0 ? venv : "python3";
}

// The floor's line 40 test, `[ -n "$CMD" ] && [ -f "$CMD" ]`: the resolver naming a run.py is not
// enough — it must be a REGULAR FILE. resolver.ts's runPy() mirrors Python's `Path.exists()`, which
// is also true for a directory named run.py, so the `-f` half is applied here (in the dispatcher,
// where bash applies it) rather than by changing resolver semantics.
function runPyFile(verb: string): string | null {
  const p = runPy(verb);
  if (!p) return null;
  try {
    return fs.statSync(p).isFile() ? p : null;
  } catch {
    return null;
  }
}

// The exit code for a signal death whose NUMBER could not be recovered (see signalNumberOf). It has
// to be a value nothing else in this system can produce, because it means "the wait status could not
// be reproduced" and must never be mistaken for one that was: outside the frozen protocol (0/2/3/4/5),
// outside a verb's own plausible exits, outside the shell conventions 126/127, and above the whole
// 128+N band for every signal that exists on either platform — including Linux's real-time signals,
// which run to 64 (128+64 = 192). 200 is the first round number clear of all of it. It is always
// accompanied by a stderr line naming the signal, so it is diagnosable rather than merely distinct.
const EXIT_UNKNOWN_SIGNAL = 200;

// Bun reports a child's death signal as a NAME, and that name is NOT trustworthy as a name: on macOS
// bun emits the LINUX name for the number it received. Measured on bun 1.3.14 / macOS arm64 for every
// catchable signal 1..31 (.orchestrate/raw/task4-fix1-bun-signal-table.log) — a child killed by macOS
// 30 (SIGUSR1) is reported "SIGPWR", by 31 (SIGUSR2) "SIGSYS", by 10 (SIGBUS) "SIGUSR1", by 12
// (SIGSYS) "SIGUSR2", by 7 (SIGEMT) "SIGBUS". Every one of those is the Linux name for that exact
// number, so the reported name is a faithful function of the true NUMBER even though it is the wrong
// name for this platform.
//
// Therefore: resolve through the LINUX table, which inverts bun's own naming and recovers the true
// number on macOS; on Linux the same table is correct by definition. Reading the PLATFORM table
// (os.constants.signals) instead is what the first implementation did, and it is actively wrong here
// — "SIGSYS" would resolve to macOS's 12, re-raising the WRONG signal, so a caller would be told the
// verb died of "bad system call" when it died of SIGUSR2. os.constants remains a fallback for names
// this table does not carry.
//
// BUN-VERSION SENSITIVITY, stated plainly: this inverts a defect. If a later bun starts emitting
// PLATFORM names, "SIGUSR2" on macOS would mean 31 while this table says 12, and the fix would become
// the bug. That is pinned by dispatch.test.ts's "recovers the true number for every signal a child
// can die of" test, which kills real children and asserts the recovered number equals the number sent
// — it goes red the moment the convention moves, in either direction.
const LINUX_SIGNAL_NUMBERS: Record<string, number> = {
  SIGHUP: 1, SIGINT: 2, SIGQUIT: 3, SIGILL: 4, SIGTRAP: 5, SIGABRT: 6, SIGBUS: 7, SIGFPE: 8,
  SIGKILL: 9, SIGUSR1: 10, SIGSEGV: 11, SIGUSR2: 12, SIGPIPE: 13, SIGALRM: 14, SIGTERM: 15,
  SIGSTKFLT: 16, SIGCHLD: 17, SIGCONT: 18, SIGSTOP: 19, SIGTSTP: 20, SIGTTIN: 21, SIGTTOU: 22,
  SIGURG: 23, SIGXCPU: 24, SIGXFSZ: 25, SIGVTALRM: 26, SIGPROF: 27, SIGWINCH: 28, SIGIO: 29,
  SIGPWR: 30, SIGSYS: 31,
};

// The true number behind bun's reported name, or null when it cannot be established (a name in
// neither table — Linux real-time signals are the realistic case). NEVER 0: signal 0 is the
// existence probe, so a 0 here would re-raise nothing and render as the meaningless exit 128.
export function signalNumberOf(name: string): number | null {
  const linux = LINUX_SIGNAL_NUMBERS[name];
  if (linux !== undefined) return linux;
  const platform = (os.constants.signals as unknown as Record<string, number>)[name];
  return typeof platform === "number" && platform > 0 ? platform : null;
}

// The one spawn. `stdio: "inherit"` hands the child our own fds, so it owns the terminal exactly as
// the floor's `exec`'d process does; cwd is inherited by omission.
//
// Exit passthrough is the subtle half. `exec` REPLACES the shell, so a verb killed by SIGTERM leaves
// the plainkeep process itself dead-by-signal: a waitpid() caller sees WIFSIGNALED (Python's
// subprocess reports returncode -15), and only a shell RENDERS that as 128+15=143. A binary that
// merely `process.exit(143)`s is therefore NOT equivalent — it exits normally, and every waitpid
// caller can tell. So a signal death is re-raised on ourselves (main.ts); 128+N is kept as the
// fallback exit code for when the re-raise does not kill us, which is what a shell would have
// reported anyway.
//
// WHAT THAT ACTUALLY ACHIEVES — the enumerated, measured result, NOT a general claim. Two rounds of
// review caught a general claim here that was false both times, so this is the whole table, measured
// end-to-end floor-vs-core on **bun 1.3.14 / macOS arm64**
// (.orchestrate/raw/task4-fix2-signal-matrix.log), over every signal that terminates by default:
//
//   REPRODUCED (15) — the dispatcher's wait status equals the floor's:
//     SIGHUP SIGINT SIGQUIT SIGTRAP SIGABRT SIGEMT SIGKILL SIGSYS SIGALRM SIGTERM SIGXCPU
//     SIGVTALRM SIGPROF SIGUSR1 SIGUSR2
//
//   PINNED DIVERGENCES (6) — the dispatcher CANNOT reproduce the floor, for two runtime reasons:
//     * bun's crash handler intercepts the re-raise and kills us with SIGTRAP (5) instead:
//       SIGILL (floor -4 / core -5), SIGFPE (-8 / -5), SIGBUS (-10 / -5), SIGSEGV (-11 / -5).
//       It ALSO dumps a bun crash report to stderr ("Bun v1.3.14 … macOS Silicon …") where the floor
//       prints nothing — the user-visible half of this divergence, and the reason the matrix pins
//       these four cells on stderr as well as on wait status. Removing that handler would need bun to
//       expose it; there is no API, and napi is barred.
//     * bun ignores the signal process-wide with no way back to SIG_DFL, so the re-raise is a no-op
//       and we take the 128+N fallback: SIGPIPE (floor -13 / core 141), SIGXFSZ (-25 / 153).
//
// All six are invisible through a shell (`$?` renders both sides the same) and visible to any
// waitpid/subprocess caller. None is reachable by plainkeep's own code — nothing under bin/ sets a
// signal disposition — but SIGSEGV/SIGBUS are reachable by a CRASHING NATIVE EXTENSION, which the
// optional search/model plane can load, and there the substituted SIGTRAP misnames the one class of
// failure where the signal is the diagnosis. Recorded in .orchestrate/field-guide.md for Phase 2.
//
// No special case is coded for any of them: the re-raise is always attempted, so if a future bun
// delivers these signals the behavior becomes correct on its own. Every cell above — agreeing and
// diverging alike — is a named case in dispatcher.json's signal-passthrough-matrix, so a change in
// EITHER direction reddens a specific test; delivery classes are additionally pinned in
// dispatch.test.ts. When a divergence cell goes red, delete its bullet here.
// The shape of what spawnSync reports back, narrowed to what the classifier needs. Kept structural
// so a test can hand it a synthetic outcome — the alternative is exhausting real file descriptors or
// process slots to reach EMFILE/EAGAIN, which is not a test anyone should run.
export interface SpawnOutcome {
  status: number | null | undefined;
  signal: string | null | undefined;
  error?: { code?: string } | undefined;
}

// Turn one spawn outcome into a CoreResult. Pure, exported, and ORDERED deliberately — the previous
// ordering asserted a cause it had not established, telling the operator "interpreter not found:
// 'python3'" for EAGAIN, EMFILE, ENOMEM and ENOEXEC alike, about an interpreter that plainly exists
// (it had already run the guardrail). Each branch below now says only what the outcome supports.
export function classifySpawnOutcome(r: SpawnOutcome, py: string): CoreResult {
  // A real exit comes first: a child that ran and returned a status is the overwhelmingly common
  // case, and its status is authoritative even when bun also populates `error` (it sets error.code
  // to the exit status on a normal non-zero exit, which is why `error` can never be the signal that
  // a spawn FAILED).
  if (r.status !== null && r.status !== undefined) {
    return { code: r.status };
  }
  // Death by signal. Tested as a NON-EMPTY string, not for truthiness: `if (r.signal)` is also false
  // for "", so a runtime that reports a signal death it cannot name would have fallen through to the
  // spawn-failure branch below and been reported as a missing interpreter.
  if (typeof r.signal === "string" && r.signal !== "") {
    const n = signalNumberOf(r.signal);
    if (n === null) {
      // Reported rather than guessed: a wrong guess re-raises the WRONG signal on ourselves.
      return {
        stderr: `plainkeep: verb killed by an unrecognized signal '${r.signal}' — its exit status cannot be reproduced`,
        code: EXIT_UNKNOWN_SIGNAL,
      };
    }
    return { code: 128 + n, signal: n };
  }
  if (r.signal !== null && r.signal !== undefined) {
    // An empty signal name: the child died by a signal the runtime declined to name. Same honest
    // dead end as above, and the reason the check above is a string test rather than a truthiness one.
    return {
      stderr: "plainkeep: verb killed by an unnamed signal — its exit status cannot be reproduced",
      code: EXIT_UNKNOWN_SIGNAL,
    };
  }
  // Neither a status nor a signal: the child never ran. Only now is an errno about the interpreter
  // meaningful, and only ENOENT actually means "not found".
  const errno = r.error?.code;
  if (errno === "ENOENT") {
    // Disclosed divergence: bash's `exec` failure prints its OWN message here
    // (`plainkeep: line 41: exec: python3: not found`, exit 127). The exit code matches; the text is
    // ours, since a bash line number is not reproducible and not worth reproducing.
    return { stderr: `plainkeep: interpreter not found: '${py}'`, code: EXIT_COMMAND_NOT_FOUND };
  }
  if (errno === "EACCES") {
    return { stderr: `plainkeep: cannot execute interpreter '${py}'`, code: EXIT_NOT_EXECUTABLE };
  }
  // Everything else — EAGAIN, EMFILE, ENFILE, ENOMEM, ENOEXEC, or nothing at all. The interpreter is
  // not accused of anything; the errno is reported verbatim so the operator can act on it, and the
  // exit code is the one that means "the dispatcher could not start the verb", never 127's "not
  // found" (which would be a diagnosis) and never a code a verb could itself return.
  return {
    stderr: `plainkeep: could not start interpreter '${py}' (${errno ?? "unknown error"})`,
    code: EXIT_SPAWN_FAILED,
  };
}

function spawnVerb(py: string, script: string, args: string[], home: string): CoreResult {
  const r = spawnSync(py, [script, ...args], {
    stdio: "inherit",
    env: { ...process.env, PLAINKEEP_HOME: home },
  });
  return classifySpawnOutcome(r as SpawnOutcome, py);
}

// The in-core interception seam Tasks 5–7 fill (`__complete`, `ui`, `mcp`; `help` stays Python per
// D6). A verb present here is answered IN-PROCESS instead of being spawned. Empty today on purpose:
// landing the seam before the first interception is written is the whole point.
//
// IT HAS TO BE HERE, not next to runCore()'s `--core-*` short-circuits in cli.ts, for two reasons —
// both of which cost correctness, not tidiness:
//
//  1. runCore() sees RAW argv, before verbFromArgv() normalizes it. Four of the six spellings that
//     mean `help` (no argv at all, `""`, `-h`, `--help`) do not literally equal "help" there, so an
//     interception written in cli.ts would catch `plainkeep help` and miss the rest — half the
//     invocations answered in-process and half spawning Python.
//  2. cli.ts is BEFORE the gate. An interception there never reaches mainCli(), so no audit line is
//     appended for a verb that ran — the "unwritten audit line" hazard the run's Global Constraints
//     name explicitly, arriving through the front door.
//
// Placed after the gate and after normalization, both properties hold by construction rather than by
// the next author remembering them. Pinned by dispatch.test.ts's INTERCEPTS tests, which assert an
// entry is reached for all six spellings AND that the audit line is written for each. The keys are
// also published through `--core-api intercepts` so the parity harness can see them without
// mirroring this file by hand.
export const INTERCEPTS: Record<string, (args: string[]) => CoreResult> = {
  // Task 5 — tab completion. complete.ts answers only what the cmd.json surface derives (the verb
  // list, actions[], enums, and the many empty answers); the moment an answer needs a live-vault
  // PROVIDER it calls the fall-through below, which is this file's own spawn path, so that case
  // costs exactly what it cost before the interception existed.
  __complete: (args) => completeIntercept(args, () => spawnPythonVerb("__complete", args)),
};

// The tail of dispatch(): resolve the verb's run.py and spawn it. Factored out (rather than left
// inline) so an interception's fall-through is literally the same code path the dispatcher would
// have taken, not a second implementation of it that can drift.
export function spawnPythonVerb(verb: string, args: string[]): CoreResult {
  const script = runPyFile(verb);
  // Byte-exact against floor line 40, which prints this and exits 4.
  if (!script) return { stderr: `plainkeep: verb '${verb}' has no run.py`, code: EXIT_NOT_FOUND };
  // dispatch() has already assigned process.env.PLAINKEEP_HOME, so this re-derivation returns that
  // same value — and an interception reached through any other caller still gets a coherent home.
  const home = resolveHome();
  return spawnVerb(pickPython(home), script, args, home);
}

// The dispatcher contract end to end. Order is the floor's and is not negotiable: the gate runs
// BEFORE resolution (so an unknown or refused verb never touches the filesystem beyond the gate's
// own reads) and resolution runs before the spawn.
export function dispatch(argv: string[]): CoreResult {
  const home = resolveHome();
  // Export it before anything reads it: guardrail.ts and resolver.ts each re-derive PLAINKEEP_HOME
  // per call, so assigning it here makes all three take the env branch and agree by construction —
  // and it is the value the child inherits (floor line 7's `export PLAINKEEP_HOME="$PK"`).
  process.env.PLAINKEEP_HOME = home;

  const { verb, args } = verbFromArgv(argv);

  // The gate (floor lines 31-35): any nonzero verdict is returned verbatim — its exit code and its
  // stderr are the guardrail's, and the audit line has already been appended by mainCli().
  const gated = mainCli([verb, ...args]);
  if (gated.code !== EXIT_OK) return gated;

  // Post-normalization, post-gate, pre-spawn — the only point where an interception keeps both the
  // audit line and every spelling of the verb. See INTERCEPTS above.
  const intercept = INTERCEPTS[verb];
  if (intercept) return intercept(args);

  return spawnPythonVerb(verb, args);
}
