// Coverage for the gate's last-resort refusal path (MIN-14). Once pythonRepr and the depth probe
// became iterative, resolver.ts throw-free and every read wrapped, no input can reach mainCli's catch
// — which is the point of the fix, but it left the branch that decides what happens when the
// enforcement layer itself breaks with zero coverage.
//
// So the throw is injected by replacing ./resolver.js with mock.module(). Bun applies module mocks
// registry-WIDE for the whole process (verified: doing it in-suite fails 14 unrelated tests, in either
// file order), so the injection runs in a CHILD `bun test` process — ./internal-error-probe.ts, named
// so the default glob skips it — and this test asserts that child's exit code. The probe carries the
// real assertions; failing them fails the child, which fails this test with the child's output.
import { test, expect } from "bun:test";
import path from "node:path";

test("the injected-throw probe passes in its own process (isolated module mock)", () => {
  const probe = path.join(import.meta.dir, "internal-error-probe.ts");
  const r = Bun.spawnSync(["bun", "test", probe], { cwd: path.join(import.meta.dir, "..", "..") });
  const output = r.stdout.toString() + r.stderr.toString();
  expect(output).toContain("1 pass");
  expect(output).toContain("0 fail");
  expect(r.exitCode).toBe(0);
});
