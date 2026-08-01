import { test, expect } from "bun:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { VERSION } from "./version.js";

// The standalone plainkeep-ui binary self-reports VERSION for the engine's offline stale-install
// check (setuplib._status_ui compares it against bin/ui/version.txt). Guard the two against drift —
// the release workflow fails on it, but catch it here first.
test("TUI VERSION matches the engine-owned pin bin/ui/version.txt", () => {
  const pinPath = fileURLToPath(new URL("../../../bin/ui/version.txt", import.meta.url));
  const pinned = readFileSync(pinPath, "utf8").trim();
  expect(VERSION).toBe(pinned);
});
