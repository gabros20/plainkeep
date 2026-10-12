#!/usr/bin/env python3
"""
run_bench.py — the retrieval bench (v2 plan step 0.0): one fixed set of queries, run against the REAL
search engine (bin/lib/indexlib.py) in up to three modes, reported as recall@5 / recall@10 / MRR per
tag. It measures; it does not judge. `test/bench/gates.json` ships EMPTY, so today a green run means
"the harness works", not "retrieval is good". Later stages (0.1 Unicode tokens, 0.2 stemming, 0.3
wider content root, ...) each move a number here and then add a gate that keeps it moved.

  keyword        FTS5 BM25 + one-hop link graph. Always runs.
  hybrid         + LanceDB vectors. Skipped (loudly) without lancedb or a working embedder.
  hybrid+rerank  + the cross-encoder. Skipped without the reranker.

How it indexes: a temp copy of the vault is the whole world. PLAINKEEP_HOME points at the copy and
`indexlib.index()` runs with NO root argument, so `CONTENT`'s default is what gets indexed: today
`<home>/wiki`, after step 0.3 the vault root. That is what lets 0.3 move the numbers. The real
vault is only ever READ (`--vault`), never used as PLAINKEEP_HOME, because `connect()` mkdirs
`.index/` and `log_query` writes `.logs/` under it.

Usage:
  python3 test/run_bench.py                          # fixture vault, all modes, tables
  python3 test/run_bench.py --json                   # one JSON object on stdout, nothing else
  python3 test/run_bench.py --modes keyword --write-baseline
  python3 test/run_bench.py --vault PATH             # the HUMAN's command; agents never run it on a real vault
  python3 test/run_bench.py --report-only            # a failed gate does not fail the run

Exit: 0 ok, 1 a gate failed or the source-vault self-test failed, 2 usage / validation error.
"""
from __future__ import annotations
import argparse
import contextlib
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BENCH = HERE / "bench"
FIXTURE = HERE / "fixtures" / "bench"
QUERIES_SCHEMA = "plainkeep.bench.queries/1"
OUT_SCHEMA = "plainkeep.bench/1"

KNOWN_TAGS = {"hu", "hu-exact", "hu-inflect", "deep", "task", "journal", "superseded",
              "unanswerable", "exact", "en"}
TAG_ORDER = ["hu", "hu-exact", "hu-inflect", "deep", "task", "journal", "superseded", "exact", "en"]
MODES = ["keyword", "hybrid", "hybrid+rerank"]
METRICS = ("recall@5", "recall@10", "mrr")
ID_RE = re.compile(r"^[a-z0-9-]+$")
COPY_IGNORE = {".git", ".index", ".logs", ".venv", ".plainkeep", "node_modules"}
EMBED_WINDOW = 2000          # indexlib._embed_text's limit; `deep` needles sit beyond it
DEEP_MIN_CHARS, DEEP_NEEDLE_MIN_OFFSET = 3000, 2500


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"\w+", _nfc(s).lower()) if len(t) > 1}


def _frontmatter(text: str) -> dict:
    """Just enough YAML for the fixture's frontmatter: `key: value` and inline `[a, b]` lists."""
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    out: dict = {}
    for line in (m.group(1).splitlines() if m else []):
        k, sep, v = line.partition(":")
        if not sep:
            continue
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            out[k.strip()] = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        else:
            out[k.strip()] = v
    return out


# --- queries + validation --------------------------------------------------------------------------
def load_json(path: Path, what: str) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(_usage(f"{what} not found: {path}"))
    except ValueError as e:
        raise SystemExit(_usage(f"{what} is not valid JSON: {path}: {e}"))


def _usage(msg: str) -> int:
    print(f"run_bench: {msg}", file=sys.stderr)
    return 2


def validate(doc: dict, root: Path, content_checks: bool) -> list[str]:
    """Every violation, not the first. Schema checks always; fixture-content checks only against the
    fixture vault (a real vault's queries are the human's, and its files are not ours to constrain)."""
    bad: list[str] = []
    if doc.get("schema") != QUERIES_SCHEMA:
        bad.append(f"schema must be {QUERIES_SCHEMA!r}, got {doc.get('schema')!r}")
    queries = doc.get("queries")
    if not isinstance(queries, list) or not queries:
        return bad + ["`queries` must be a non-empty list"]
    seen: set[str] = set()
    for i, q in enumerate(queries):
        if not isinstance(q, dict):
            bad.append(f"queries[{i}]: not an object")
            continue
        qid = q.get("id")
        where = f"{qid}" if isinstance(qid, str) else f"queries[{i}]"
        if not isinstance(qid, str) or not ID_RE.match(qid):
            bad.append(f"{where}: id must match [a-z0-9-]+")
        elif qid in seen:
            bad.append(f"{where}: duplicate id")
        else:
            seen.add(qid)
        text, expected, tags = q.get("query"), q.get("expected"), q.get("tags")
        if not isinstance(text, str) or not text.strip():
            bad.append(f"{where}: query must be a non-empty string")
            text = ""
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            bad.append(f"{where}: tags must be a list of strings")
            tags = []
        if not isinstance(expected, list) or not all(isinstance(e, str) for e in expected):
            bad.append(f"{where}: expected must be a list of paths")
            expected = []
        if not isinstance(q.get("answerable"), bool):
            bad.append(f"{where}: answerable must be a boolean")
        elif q["answerable"] != bool(expected):
            bad.append(f"{where}: answerable={q['answerable']} but expected has {len(expected)} entries")
        if len(expected) > 2:
            bad.append(f"{where}: expected has {len(expected)} entries (max 2)")
        for e in expected:
            if not e.endswith(".md") or e.startswith("/") or ".." in Path(e).parts or "\\" in e:
                bad.append(f"{where}: expected path {e!r} must be vault-relative POSIX ending in .md")
        for t in tags:
            if t not in KNOWN_TAGS:
                bad.append(f"{where}: unknown tag {t!r}")
        if "hu" not in tags and "en" not in tags:
            bad.append(f"{where}: a non-hu query must carry the `en` tag")
        needle = q.get("needle")
        if needle is not None and not isinstance(needle, str):
            bad.append(f"{where}: needle must be a string")
        if "deep" in tags and not needle:
            bad.append(f"{where}: a `deep` query needs a `needle`")
        if not content_checks or not expected:
            continue
        files = {}
        for e in expected:
            f = root / e
            if not f.is_file():
                bad.append(f"{where}: expected file does not exist in the fixture: {e}")
            else:
                files[e] = _nfc(f.read_text(encoding="utf-8"))
        if not files:
            continue
        target = files.get(expected[0])
        if "hu" in tags and not any(c.isalpha() and ord(c) > 127 for c in text):
            bad.append(f"{where}: `hu` query has no non-ASCII letter")
        if "hu-inflect" in tags:
            for e, body in files.items():
                clash = sorted(_tokens(text) & _tokens(body))
                if clash:
                    bad.append(f"{where}: `hu-inflect` query word(s) {clash} occur verbatim in {e}")
        if needle and target is not None and needle not in target:
            bad.append(f"{where}: needle is not a verbatim substring of {expected[0]}")
        if "deep" in tags and target is not None:
            if len(target) <= DEEP_MIN_CHARS:
                bad.append(f"{where}: `deep` target is {len(target)} chars (must be > {DEEP_MIN_CHARS})")
            at = target.find(needle) if needle else -1
            if at != -1 and at < DEEP_NEEDLE_MIN_OFFSET:
                bad.append(f"{where}: `deep` needle starts at offset {at} (must be >= "
                           f"{DEEP_NEEDLE_MIN_OFFSET}, past the {EMBED_WINDOW}-char embed window)")
        if "task" in tags and not expected[0].startswith("tasks/"):
            bad.append(f"{where}: `task` target must live under tasks/")
        if "journal" in tags and not expected[0].startswith("journal/"):
            bad.append(f"{where}: `journal` target must live under journal/")
        if "superseded" in tags and target is not None \
                and _frontmatter(target).get("status") != "superseded":
            bad.append(f"{where}: `superseded` target must have `status: superseded`")
        if "exact" in tags and target is not None:
            fm = _frontmatter(target)
            names = [fm.get("title", "")] + list(fm.get("aliases") or [])
            if _nfc(text).casefold() not in {_nfc(n).casefold() for n in names}:
                bad.append(f"{where}: `exact` query must equal the target's title or an alias")
    return bad


def load_gates(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    doc = load_json(path, "gates file")
    gates = doc.get("gates")
    if not isinstance(gates, list):
        raise SystemExit(_usage(f"{path}: `gates` must be a list"))
    for g in gates:
        ok = (isinstance(g, dict) and g.get("mode") in MODES and g.get("metric") in METRICS
              and isinstance(g.get("tag"), str) and isinstance(g.get("min"), (int, float)))
        if not ok:
            raise SystemExit(_usage(f"{path}: bad gate {g!r} (need mode, tag, metric, min)"))
    return gates


# --- the source-vault self-test ----------------------------------------------------------------------
def snapshot(root: Path) -> dict:
    """Names + mtimes (+ sizes) of everything under root. A change between two snapshots means the
    bench wrote into the tree it was supposed to only read."""
    snap = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            p = Path(dirpath) / name
            st = p.lstat()
            snap[p.relative_to(root).as_posix()] = (st.st_mtime_ns, st.st_size)
    return snap


def selftest_validator(doc: dict, root: Path) -> str | None:
    """Prove the validator can fail: corrupt a copy of a `hu-inflect` query by adding a word that IS
    in its target, and require a violation for it. Returns an error string, or None when it works."""
    victim = next((q for q in doc["queries"] if "hu-inflect" in q.get("tags", []) and q.get("expected")), None)
    if victim is None:
        return None
    body = _nfc((root / victim["expected"][0]).read_text(encoding="utf-8"))
    word = sorted(t for t in _tokens(body) if len(t) > 4)[0]
    bad = copy.deepcopy(doc)
    for q in bad["queries"]:
        if q["id"] == victim["id"]:
            q["query"] += " " + word
    found = validate(bad, root, True)
    if not any(victim["id"] in v and "hu-inflect" in v for v in found):
        return f"validator accepted a corrupted hu-inflect query ({victim['id']} + {word!r})"
    return None


# --- the vault copy ---------------------------------------------------------------------------------
def copy_vault(src: Path, dst: Path, md_only: bool) -> None:
    if not md_only:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*COPY_IGNORE))
        return
    dst.mkdir(parents=True)
    for dirpath, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in COPY_IGNORE]
        for name in files:
            s = Path(dirpath) / name
            if name.endswith(".md") and not s.is_symlink():
                t = dst / s.relative_to(src)
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(s, t)


# --- running + scoring ----------------------------------------------------------------------------------
class Engine:
    """indexlib bound to ONE temp vault. The env is set BEFORE the import because indexlib and
    vectorstore fix PLAINKEEP_HOME / CONTENT / DB / LANCE_DIR at import time."""

    def __init__(self, home: Path):
        self.home = home
        os.environ["PLAINKEEP_HOME"] = str(home)
        os.environ.pop("PLAINKEEP_CONTENT", None)
        os.environ["PLAINKEEP_VECTORS"] = "0"
        os.environ["PLAINKEEP_RERANK"] = "0"
        sys.path.insert(0, str(REPO / "bin" / "lib"))
        import indexlib
        self.ix = indexlib
        self._hybrid: str | None | bool = False   # False = not tried; None = ok; str = skip reason

    def content_root(self) -> str:
        return Path(self.ix.CONTENT).resolve().relative_to(self.home.resolve()).as_posix()

    def index(self) -> int:
        with contextlib.redirect_stdout(sys.stderr):    # index chatter must not reach --json stdout
            return self.ix.index(verbose=False)

    def vault_rel(self, p: str) -> str:
        try:
            return (Path(self.ix.CONTENT) / p).resolve().relative_to(self.home.resolve()).as_posix()
        except ValueError:
            return p

    def set_mode(self, vectors: bool, rerank: bool) -> None:
        os.environ["PLAINKEEP_VECTORS"] = "1" if vectors else "0"
        os.environ["PLAINKEEP_RERANK"] = "1" if rerank else "0"

    def hybrid_skip_reason(self) -> str | None:
        """Index with vectors on, once; None when hybrid is genuinely usable, else why not."""
        if self._hybrid is not False:
            return self._hybrid
        self.set_mode(True, False)
        reason: str | None = None
        try:
            import embed
            import vectorstore
        except Exception as e:
            reason = f"embed/vectorstore import failed: {e}"
        else:
            if not vectorstore.available():
                reason = "lancedb not importable"
            elif not embed.available():
                reason = "embedder unavailable"
            else:
                n = self.index()
                try:
                    have = vectorstore.count()
                except Exception as e:
                    have, reason = 0, f"vector store unreadable: {e}"
                # index() swallows a failed vector pass and carries on keyword-only; without this
                # check "hybrid" would silently equal "keyword".
                if reason is None and have < n:
                    reason = f"vector pass incomplete ({have} vectors for {n} indexed files)"
        self._hybrid = reason
        return reason

    def rerank_skip_reason(self) -> str | None:
        if self.hybrid_skip_reason():
            return "hybrid skipped"
        try:
            import rerank
            return None if rerank.available() else "reranker unavailable"
        except Exception as e:
            return f"reranker import failed: {e}"

    def run(self, queries: list[dict]) -> list[dict]:
        rows = []
        for q in queries:
            hits = [self.vault_rel(p) for p, _h, _s in self.ix.search(q["query"], k=10)]
            exp = q["expected"]
            rank = next((i for i, h in enumerate(hits, 1) if exp and h == exp[0]), None)
            if exp and rank is None:
                rank = next((i for i, h in enumerate(hits, 1) if h in exp), None)
            rows.append({"q": q, "hits": hits, "rank": rank,
                         "r5": len(set(exp) & set(hits[:5])) / len(exp) if exp else None,
                         "r10": len(set(exp) & set(hits[:10])) / len(exp) if exp else None})
        return rows


def _agg(rows: list[dict]) -> dict:
    n = len(rows)
    return {"n": n,
            "recall@5": round(sum(r["r5"] for r in rows) / n, 3),
            "recall@10": round(sum(r["r10"] for r in rows) / n, 3),
            "mrr": round(sum(1 / r["rank"] if r["rank"] else 0 for r in rows) / n, 3)}


def summarize(rows: list[dict]) -> dict:
    ans = [r for r in rows if r["q"]["answerable"]]
    una = [r for r in rows if not r["q"]["answerable"]]
    tags = sorted({t for r in ans for t in r["q"]["tags"]})
    return {"status": "ran",
            "overall": _agg(ans) if ans else None,
            "by_tag": {t: _agg([r for r in ans if t in r["q"]["tags"]]) for t in tags},
            "unanswerable": {"n": len(una), "empty_top5": sum(1 for r in una if not r["hits"][:5])},
            "per_query": [{"id": r["q"]["id"], "rank": r["rank"], "top5": r["hits"][:5]} for r in rows]}


def evaluate_gates(gates: list[dict], modes: dict) -> list[dict]:
    out = []
    for g in gates:
        res = dict(g)
        m = modes.get(g["mode"])
        if not m or m["status"] != "ran":
            res.update(status="skipped", value=None)
        else:
            src = m["overall"] if g["tag"] == "*" else m["by_tag"].get(g["tag"])
            val = src.get(g["metric"]) if src else None
            res.update(value=val, status="fail" if val is None or val < g["min"] else "pass")
        out.append(res)
    return out


# --- output -----------------------------------------------------------------------------------------
def _f(v) -> str:
    return "-" if v is None else f"{v:.3f}"


def _with_delta(v, base) -> str:
    return _f(v) if base is None or v is None else f"{v:.3f} ({v - base:+.3f})"


def render(report: dict, baseline: dict | None) -> list[str]:
    modes = report["modes"]
    ran = [m for m in modes if modes[m]["status"] == "ran"]
    lines = [f"retrieval bench — source: {report['source']}, content root: {report['content_root']!r}, "
             f"{report['queries']} queries", ""]
    lines.append(f"{'mode':<15}{'R@5':<18}{'R@10':<18}{'MRR':<18}n")
    for m in modes:
        r = modes[m]
        if r["status"] != "ran":
            lines.append(f"{m:<15}SKIP  {r['reason']}")
            continue
        b = ((baseline or {}).get("modes", {}).get(m) or {})
        bo = b.get("overall") if b.get("status") == "ran" else None
        o = r["overall"] or {}
        cells = [_with_delta(o.get(k), (bo or {}).get(k)) for k in METRICS]
        lines.append(f"{m:<15}{cells[0]:<18}{cells[1]:<18}{cells[2]:<18}{o.get('n', 0)}")
    if ran:
        tags = [t for t in TAG_ORDER if any(t in modes[m]["by_tag"] for m in ran)]
        lines += ["", "recall@5 by tag" + ("   (baseline delta is in the table above)" if baseline else "")]
        lines.append(f"{'tag':<15}{'n':<5}" + "".join(f"{m:<15}" for m in ran))
        for t in tags:
            n = next(modes[m]["by_tag"][t]["n"] for m in ran if t in modes[m]["by_tag"])
            lines.append(f"{t:<15}{n:<5}" + "".join(
                f"{_f((modes[m]['by_tag'].get(t) or {}).get('recall@5')):<15}" for m in ran))
        if "exact" in tags:
            lines.append(f"{'exact (MRR)':<20}" + "".join(
                f"{_f((modes[m]['by_tag'].get('exact') or {}).get('mrr')):<15}" for m in ran))
        lines += ["", "unanswerable (informational; excluded from every aggregate above)"]
        for m in ran:
            u = modes[m]["unanswerable"]
            lines.append(f"  {m:<15}n={u['n']}  empty_top5={u['empty_top5']}")
    lines += ["", "gates"]
    if not report["gates"]:
        lines.append("  (none defined)")
    for g in report["gates"]:
        lines.append(f"  {g['status'].upper():<8}{g['mode']} / {g['tag']} / {g['metric']} >= {g['min']}"
                     f"  (value {_f(g['value'])}, since {g.get('since', '?')})")
    return lines


def git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


# --- main ---------------------------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Retrieval bench (v2 plan step 0.0).")
    ap.add_argument("--vault", help="bench a REAL vault, read-only (the human's command)")
    ap.add_argument("--queries", help="queries file for --vault (default test/bench/queries.private.json)")
    ap.add_argument("--modes", help="comma list of keyword,hybrid,hybrid+rerank "
                                    "(default: all for the fixture, keyword for --vault)")
    ap.add_argument("--json", action="store_true", help="one JSON object on stdout, nothing else")
    ap.add_argument("--write-baseline", action="store_true", help="record test/bench/baseline.json (fixture only)")
    ap.add_argument("--report-only", action="store_true", help="a failed gate does not fail the run")
    a = ap.parse_args(argv)

    vault_mode = a.vault is not None
    if vault_mode and a.write_baseline:
        return _usage("--write-baseline is refused with --vault (the baseline is fixture-only)")
    if a.modes:
        modes_wanted = [m.strip() for m in a.modes.split(",") if m.strip()]
        unknown = [m for m in modes_wanted if m not in MODES]
        if unknown or not modes_wanted:
            return _usage(f"unknown mode(s) {unknown or a.modes!r}; choose from {', '.join(MODES)}")
    else:
        modes_wanted = ["keyword"] if vault_mode else list(MODES)
    modes_wanted = [m for m in MODES if m in modes_wanted]

    if vault_mode:
        source = Path(a.vault).expanduser()
        if not source.is_dir():
            return _usage(f"--vault is not a directory: {source}")
        qfile = Path(a.queries) if a.queries else BENCH / "queries.private.json"
        if not qfile.is_file():
            return _usage(f"queries file not found: {qfile}\n  write your own queries there (same schema "
                          f"as test/bench/queries.json; this file is gitignored), or pass --queries FILE")
    else:
        source, qfile = FIXTURE, BENCH / "queries.json"

    doc = load_json(qfile, "queries file")
    problems = validate(doc, source, content_checks=not vault_mode)
    if problems:
        print(f"run_bench: {len(problems)} query violation(s) in {qfile}:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2
    queries = doc["queries"]
    gates = [] if vault_mode else load_gates(BENCH / "gates.json")

    before = snapshot(source)
    modes: dict = {}
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "vault"
        copy_vault(source, home, md_only=vault_mode)
        eng = Engine(home)
        content_root = eng.content_root()
        for mode in modes_wanted:
            if mode == "keyword":
                eng.set_mode(False, False)
                eng.index()
            else:
                reason = eng.hybrid_skip_reason() if mode == "hybrid" else eng.rerank_skip_reason()
                if reason:
                    modes[mode] = {"status": "skipped", "reason": reason}
                    continue
                eng.set_mode(True, mode == "hybrid+rerank")
            modes[mode] = summarize(eng.run(queries))
    after = snapshot(source)

    # self-test: the source vault is untouched, and the validator can actually fail.
    selftest_errors = []
    if before != after:
        changed = sorted({k for k in before.keys() | after.keys() if before.get(k) != after.get(k)})
        selftest_errors.append(f"bench wrote into its source vault ({source}): {changed[:5]}")
    if not vault_mode:
        err = selftest_validator(doc, source)
        if err:
            selftest_errors.append(err)

    results = evaluate_gates(gates, modes)
    report = {"schema": OUT_SCHEMA, "source": "vault" if vault_mode else "fixture",
              "content_root": content_root, "queries": len(queries), "modes": modes, "gates": results}

    baseline = None
    bfile = BENCH / "baseline.json"
    if not vault_mode and bfile.is_file():
        try:
            baseline = json.loads(bfile.read_text(encoding="utf-8"))
        except ValueError:
            baseline = None

    # human output goes to stdout; with --json everything but the JSON goes to stderr
    side = sys.stderr if a.json else sys.stdout
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("\n".join(render(report, baseline)))
    for mode, r in modes.items():
        if r["status"] == "skipped":
            print(f"SUITE-NOTE: bench: {mode} skipped ({r['reason']})", file=side)
    for g in results:
        if g["status"] == "skipped":
            print(f"SUITE-NOTE: bench: gate {g['mode']}/{g['tag']}/{g['metric']} skipped "
                  f"(mode {g['mode']} did not run)", file=side)
    for e in selftest_errors:
        print(f"FAIL self-test: {e}", file=side)

    if a.write_baseline:
        out = dict(report)
        out["commit"] = git_commit()
        bfile.write_text(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(f"baseline written: {bfile.relative_to(REPO)} (modes: {', '.join(modes)})", file=side)

    failed_gates = sum(1 for g in results if g["status"] == "fail")
    failed = failed_gates + (1 if selftest_errors else 0)
    total = len(results) + 1
    print(f"\nResult: {total - failed} passed, {failed} failed, {total} checks", file=side)
    gate_failures_count = 0 if a.report_only else failed_gates
    return 1 if (gate_failures_count or selftest_errors) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
