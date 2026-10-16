#!/usr/bin/env python3
"""run_hostconfig.py — the host config file (v2 0.5): precedence flag/env > config > default, the clean-
environment subprocess that gets vectors on through the config alone, `setup search --yes` writing the
config, `setup status` naming the effective value and its source, and doctor's env.sh suggestion."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from lib.hermetic import scratch_root, seal
seal()   # hermetic: PLAINKEEP_CONFIG_HOME is a throwaway dir, so the real ~/.config/plainkeep is never touched

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin" / "lib"))
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))


def main() -> int:
    import hostconfig as hc
    cfgdir = Path(os.environ["PLAINKEEP_CONFIG_HOME"])
    check("config path follows PLAINKEEP_CONFIG_HOME (the seal)", hc.path() == cfgdir / "config.toml", str(hc.path()))
    knobs = [e for e, _ in hc.KEYS.values()]
    for k in knobs:
        os.environ.pop(k, None)

    check("no file → default", hc.resolve("search.vectors", False) == (False, "default"))
    check("flag() false without config", hc.flag("search.vectors") is False)

    (cfgdir / "config.toml").write_text(
        '[search]\nvectors = true\nrerank = false  # off\n[models]\nenrich = "gemma4:e2b"\n'
        '[ollama]\nhost = "http://gpu-box:11434"\n')
    check("config supplies a bool", hc.resolve("search.vectors", False) == (True, "config"))
    check("config supplies a string", hc.resolve("models.enrich", "x") == ("gemma4:e2b", "config"))
    check("config supplies the ollama host", hc.ollama_host() == "http://gpu-box:11434")
    check("unset key falls to the default", hc.resolve("models.triage", "d") == ("d", "default"))
    os.environ["PLAINKEEP_VECTORS"] = "0"
    check("env beats config (explicit off)", hc.resolve("search.vectors", True) == (False, "env"))
    os.environ["PLAINKEEP_VECTORS"] = ""
    check("empty env is unset → config", hc.resolve("search.vectors", False) == (True, "config"))
    os.environ.pop("PLAINKEEP_VECTORS")
    os.environ["PLAINKEEP_ENRICH_MODEL"] = "other:1b"
    check("env beats config for strings", hc.resolve("models.enrich") == ("other:1b", "env"))
    os.environ.pop("PLAINKEEP_ENRICH_MODEL")

    check("fallback reader (py3.10) agrees with tomllib",
          hc._mini_toml((cfgdir / "config.toml").read_text()) ==
          {"search": {"vectors": True, "rerank": False}, "models": {"enrich": "gemma4:e2b"},
           "ollama": {"host": "http://gpu-box:11434"}})
    (cfgdir / "config.toml").write_text("this is [not toml")
    check("malformed file is an empty config, never an error", hc.load() == {})

    (cfgdir / "config.toml").unlink()
    hc.set_values({"search.vectors": True})
    hc.set_values({"models.triage": 'a"b'})
    check("set_values merges and round-trips",
          hc.load() == {"search.vectors": True, "models.triage": 'a"b'}, str(hc.load()))
    try:
        hc.set_values({"bogus.key": 1})
        check("set_values rejects unknown keys", False)
    except KeyError:
        check("set_values rejects unknown keys", True)

    # --- the Done-when: a non-interactive process with a CLEAN environment gets vectors via config alone
    code = ("import sys; sys.path.insert(0, %r); import indexlib, agent, enrichlib; "
            "print(indexlib._vectors_on(), indexlib._rerank_on(), agent.triage_model(), enrichlib.OLLAMA_HOST)"
            % str(REPO / "bin" / "lib"))
    hc.set_values({"search.rerank": True, "ollama.host": "http://h:1"})
    # the engine modules resolve a vault at import; PLAINKEEP_HOME is the only variable beyond PATH
    clean = {"PATH": os.environ.get("PATH", ""), "PLAINKEEP_CONFIG_HOME": str(cfgdir),
             "PLAINKEEP_HOME": scratch_root()}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=clean)
    check("clean-env subprocess: vectors+rerank on, triage and host from config alone",
          r.stdout.strip() == "True True a\"b http://h:1", r.stdout + r.stderr)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**clean, "PLAINKEEP_VECTORS": "0"})
    check("env override still wins in a subprocess", r.stdout.startswith("False True"), r.stdout + r.stderr)

    # --- setup search --yes writes the config; setup status prints value + source
    (cfgdir / "config.toml").unlink()
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        env = {**os.environ, "PLAINKEEP_HOME": str(home), "PYTHONPATH": str(REPO / "bin"),
               "PLAINKEEP_SETUP_FAKE": "1", "PLAINKEEP_ASSUME_OLLAMA": "1"}
        r = subprocess.run([sys.executable, str(REPO / "bin" / "setup" / "run.py"), "search", "--yes"],
                           capture_output=True, text=True, env=env)
        check("setup search --yes exits 0", r.returncode == 0, r.stdout + r.stderr)
        check("setup search --yes wrote search.vectors = true", hc.load().get("search.vectors") is True,
              str(hc.load()) + r.stderr)
        r = subprocess.run([sys.executable, str(REPO / "bin" / "setup" / "run.py"), "--json"],
                           capture_output=True, text=True, env=env)
        check("setup status names the effective value and its source",
              "search.vectors = on (config)" in r.stdout, r.stdout[-400:] + r.stderr)

    # --- doctor's env.sh migration hint (suggest only)
    (cfgdir / "env.sh").write_text("export PLAINKEEP_VECTORS=1\nexport PLAINKEEP_RERANK=1\n")
    check("env.sh not redundant while config lacks a value", hc.redundant_env_sh() is None)
    hc.set_values({"search.rerank": True})
    check("env.sh redundant once config holds the same values", hc.redundant_env_sh() == cfgdir / "env.sh")
    check("env.sh is never deleted", (cfgdir / "env.sh").exists())

    print(f"{BOLD}Host config (v2 0.5) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<60}" + (f" {DIM}{str(detail).strip()[:160]}{RESET}" if (detail and not ok) else ""))
    failed = len(results) - passed
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, {len(results)} checks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
