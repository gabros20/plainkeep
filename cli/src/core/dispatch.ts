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
import { EXIT_NOT_FOUND, EXIT_OK, mainCli } from "./guardrail.js";
import { runPy } from "./resolver.js";

// Exit codes for a spawn that never became a child. OFF the frozen gate protocol (0/2/3/4/5) by
// design — these are the shell's own conventions, and the floor reaches them through bash's `exec`
// failure path rather than through any plainkeep decision: 127 = not found, 126 = found but not
// executable. See spawnVerb() for the disclosed text divergence.
const EXIT_NOT_EXECUTABLE = 126;
const EXIT_COMMAND_NOT_FOUND = 127;

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
// caller can tell. So a signal death is re-raised on ourselves (main.ts), reproducing the floor's
// wait status for every signal a verb can actually die of; 128+N is kept as the fallback exit code
// for the case where the re-raise does not kill us, which is what a shell would have reported anyway.
//
// The one signal that always takes that fallback is SIGPIPE: the bun runtime ignores it process-wide
// and offers no way back to SIG_DFL without native code, so re-raising is a no-op and the dispatcher
// exits 141 where the floor dies by 13. A DOCUMENTED divergence, invisible from a shell ($?=141 both
// ways) and visible to a waitpid caller, pinned in both directions by dispatcher.json's
// signal-sigpipe-divergence case and by a dispatch.test.ts test — if a future bun stops ignoring
// SIGPIPE, both go red and this comment is what should be deleted.
function spawnVerb(py: string, script: string, args: string[], home: string): CoreResult {
  const r = spawnSync(py, [script, ...args], {
    stdio: "inherit",
    env: { ...process.env, PLAINKEEP_HOME: home },
  });
  if (r.signal) {
    const n = signalNumberOf(r.signal);
    if (n === null) {
      // Unreachable for any signal in the table above; reported rather than guessed, because a wrong
      // guess here re-raises the WRONG signal on ourselves.
      return {
        stderr: `plainkeep: verb killed by an unrecognized signal '${r.signal}' — its exit status cannot be reproduced`,
        code: EXIT_UNKNOWN_SIGNAL,
      };
    }
    return { code: 128 + n, signal: n };
  }
  if (r.status !== null && r.status !== undefined) {
    return { code: r.status };
  }
  // No status and no signal: the child never existed. Bun sets `error` even on a normal non-zero
  // exit (its `code` is then the exit status), so the ABSENCE of both status and signal — not the
  // presence of `error` — is what identifies a spawn failure.
  const code = (r.error as NodeJS.ErrnoException | undefined)?.code;
  if (code === "EACCES") {
    return { stderr: `plainkeep: cannot execute interpreter '${py}'`, code: EXIT_NOT_EXECUTABLE };
  }
  // Disclosed divergence: bash's `exec` failure prints its OWN message here
  // (`plainkeep: line 41: exec: python3: not found`, exit 127). The exit code matches; the text is
  // ours, since a bash line number is not reproducible and not worth reproducing. Unreachable in
  // practice — a plainkeep with no python3 on PATH cannot have run its guardrail either.
  return { stderr: `plainkeep: interpreter not found: '${py}'`, code: EXIT_COMMAND_NOT_FOUND };
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

  const script = runPyFile(verb);
  // Byte-exact against floor line 40, which prints this and exits 4.
  if (!script) return { stderr: `plainkeep: verb '${verb}' has no run.py`, code: EXIT_NOT_FOUND };

  return spawnVerb(pickPython(home), script, args, home);
}
