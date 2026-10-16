#!/usr/bin/env python3
"""
plainkeep models list | status | stop [--all] [<model>] | pull [--stage <s>|--all] --yes
        | test <stage> [--model <m>] [--input <file>] --yes — the download/offload/on-demand test
surface for the model layer behind every stage (search-enrichment proposal §2.1). Model choice is env
config (PLAINKEEP_OCR/PLAINKEEP_VLM/PLAINKEEP_ENRICH_MODEL/PLAINKEEP_EMBED_MODEL/PLAINKEEP_RERANK_MODEL); this verb lets you SEE what
each stage is configured to use, whether it's actually pulled/available, offload a resident model, and
A/B a candidate on a sample without wiring anything — adopt it after by setting its env var.

Risk: safe_write at the surface so `list`/`status` (read) and `stop` (unloads a model — reloads on
next use, no data loss) stay free of --yes. The two TRANSMITTING/downloading subactions self-gate:
`pull` and `test` can each pull gigabytes, so both refuse without --yes -> EXIT_CONFIRM(3), mirroring
how `plainkeep share`/`plainkeep backup` gate their own transmitting subactions.

Every real model call goes over urllib to the local Ollama daemon (bin/lib/embed.py's pattern) or an
existing lib's own dispatch (imagelib for ocr/vlm, embed for embed, enrichlib for enrich, rerank for
rerank) — never `import ollama`. `list`/`status` never load a model or hit the network: stage
availability is decided by cheap probes (importlib.find_spec, `ollama list`/`ollama ps`), same
discipline as imagelib's `_has_*` probes.
"""
from __future__ import annotations
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import agent, embed, enrichlib, imagelib, output, rerank  # noqa: E402

STAGES = ("stt", "ocr", "vlm", "enrich", "triage", "embed", "rerank")


def _have_mod(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


# --------------------------------------------------------------------------- ollama CLI (probe-only; never a model load)

def _ollama_models() -> list[str] | None:
    """`ollama list` model tags, or None if the CLI/daemon is unreachable (never crashes)."""
    if not shutil.which("ollama"):
        return None
    try:
        r = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    lines = r.stdout.strip().splitlines()
    return [ln.split()[0] for ln in lines[1:] if ln.strip()]


def _ollama_has(model: str) -> bool:
    models = _ollama_models()
    if models is None:
        return False
    base = model.split(":")[0]
    return any(m == model or m.split(":")[0] == base for m in models)


def _ollama_ps() -> list[dict] | None:
    """Resident models via `ollama ps`; None if the CLI/daemon is unreachable."""
    if not shutil.which("ollama"):
        return None
    try:
        r = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    rows = []
    for ln in r.stdout.strip().splitlines()[1:]:
        parts = ln.split(None, 4)  # NAME ID SIZE PROCESSOR UNTIL (UNTIL has embedded spaces)
        if len(parts) >= 5:
            rows.append({"name": parts[0], "id": parts[1], "size": parts[2],
                        "processor": parts[3], "until": parts[4]})
    return rows


def _ollama_model_for(stage: str) -> str | None:
    """The ollama tag `pull`/`test` would fetch for `stage`, or None if the stage isn't ollama-backed
    under the current config (stt/rerank use their own pip-managed runtimes; ocr's mlx/apple/tesseract
    backends auto-fetch or ship their own weights)."""
    if stage == "vlm":
        m = (os.environ.get("PLAINKEEP_VLM", "qwen3-vl:4b") or "qwen3-vl:4b").strip()
        return None if m.lower() == "none" else m
    if stage == "enrich":
        return enrichlib.DEFAULT_MODEL
    if stage == "triage":
        return agent.triage_model()
    if stage == "embed":
        return os.environ.get("PLAINKEEP_EMBED_MODEL", "embeddinggemma")
    if stage == "ocr":
        m = (os.environ.get("PLAINKEEP_OCR", "auto") or "auto").strip().lower()
        return m if m not in ("auto", "none", "apple", "tesseract", "glm-ocr") else None
    return None  # stt, rerank: not ollama-backed


# --------------------------------------------------------------------------- per-stage info (list)

def _vlm_backend(model: str) -> tuple[str, bool]:
    if model.lower() == "none":
        return "none", False
    mlx_enabled = (os.environ.get("PLAINKEEP_MLX", "auto") or "auto").strip().lower() != "off"
    status = imagelib.backends_status()
    if mlx_enabled and status["mlx_vlm"]:
        return "mlx-vlm", True  # HF auto-fetches the model on first use
    if status["ollama"]:
        return "ollama", _ollama_has(model)
    return "none", False


def _stage_info(stage: str) -> dict:
    if stage == "stt":
        # S1 (PLAINKEEP_STT_MODEL retrofit) is not shipped yet — report the cascade files/run.py._tier_audio
        # actually runs (`plainkeep help models` links back to the proposal), env read for forward-compat.
        if _have_mod("parakeet_mlx"):
            model, runtime, available = "parakeet-tdt-0.6b-v2", "mlx (parakeet-mlx)", True
        elif _have_mod("mlx_whisper"):
            model, runtime, available = "whisper", "mlx-whisper", True
        elif _have_mod("faster_whisper"):
            model, runtime, available = "base", "faster-whisper", True
        else:
            model, runtime, available = "-", "none", False
        model = os.environ.get("PLAINKEEP_STT_MODEL") or model
        return {"stage": stage, "model": model, "runtime": runtime, "available": available}
    if stage == "ocr":
        model = (os.environ.get("PLAINKEEP_OCR", "auto") or "auto").strip().lower()
        runtime = imagelib.ocr_backend_label() or "none"
        return {"stage": stage, "model": model, "runtime": runtime, "available": runtime != "none"}
    if stage == "vlm":
        model = (os.environ.get("PLAINKEEP_VLM", "qwen3-vl:4b") or "qwen3-vl:4b").strip()
        runtime, available = _vlm_backend(model)
        return {"stage": stage, "model": model, "runtime": runtime, "available": available}
    if stage == "enrich":
        model = enrichlib.DEFAULT_MODEL
        return {"stage": stage, "model": model, "runtime": "ollama", "available": _ollama_has(model)}
    if stage == "triage":
        model = agent.triage_model()
        return {"stage": stage, "model": model, "runtime": "ollama", "available": _ollama_has(model)}
    if stage == "embed":
        model = os.environ.get("PLAINKEEP_EMBED_MODEL", embed.model_name())
        return {"stage": stage, "model": model, "runtime": "ollama", "available": _ollama_has(model)}
    # rerank
    model = os.environ.get("PLAINKEEP_RERANK_MODEL") or "(auto-picked)"
    if _have_mod("fastembed"):
        runtime, available = "fastembed", True
    elif _have_mod("sentence_transformers"):
        runtime, available = "sentence-transformers", True
    else:
        runtime, available = "none", False
    return {"stage": stage, "model": model, "runtime": runtime, "available": available}


def cmd_list():
    rows = [_stage_info(s) for s in STAGES]

    def render(rs):
        lines = [f"{'stage':<8} {'model':<24} {'runtime':<18} available"]
        for r in rs:
            lines.append(f"{r['stage']:<8} {r['model']:<24} {r['runtime']:<18} {'yes' if r['available'] else 'no'}")
        return "\n".join(lines)

    return output.emit_rows(rows, "models", human=render)


def cmd_status():
    rows = _ollama_ps()
    if rows is None:
        return output.emit_rows([], "models", header={"ollama": False},
                                human=lambda _: "ollama not available — no resident models to show")

    def render(rs):
        if not rs:
            return "no resident models"
        return "\n".join(f"  {r['name']:<24} {r['size']:<10} {r['processor']:<12} {r['until']}" for r in rs)

    return output.emit_rows(rows, "models", header={"ollama": True}, human=render)


def cmd_stop(argv):
    all_ = "--all" in argv
    model = next((a for a in argv if not a.startswith("-")), None)
    if not all_ and not model:
        output.fail(output.EXIT_USAGE, "usage: plainkeep models stop [--all] [<model>]", verb="models")
    if not shutil.which("ollama"):
        output.fail(output.EXIT_UNEXPECTED, "ollama not installed", hint="brew install ollama", verb="models")
    if all_:
        targets = [r["name"] for r in (_ollama_ps() or [])]
    else:
        targets = [model]
    stopped = []
    for m in targets:  # pragma: no cover - talks to a live ollama daemon, never exercised in tests
        r = subprocess.run(["ollama", "stop", m], capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            stopped.append(m)
    data = {"stopped": stopped}
    return output.emit(data, "models", human=lambda d: f"stopped: {', '.join(d['stopped']) or '(none)'}")


def cmd_pull(argv):
    yes = ("--yes" in argv) or ("-y" in argv)
    all_ = "--all" in argv
    stage = argv[argv.index("--stage") + 1] if "--stage" in argv else None
    if not all_ and not stage:
        output.fail(output.EXIT_USAGE, "usage: plainkeep models pull [--stage <s>|--all] --yes", verb="models")
    stages = list(STAGES) if all_ else [stage]
    for s in stages:
        if s not in STAGES:
            output.fail(output.EXIT_USAGE, f"unknown stage '{s}' (one of: {', '.join(STAGES)})", verb="models")
    targets = [(s, _ollama_model_for(s)) for s in stages]
    pullable = [(s, m) for s, m in targets if m]
    skipped = [s for s, m in targets if not m]
    if not yes:
        names = ", ".join(f"{s}:{m}" for s, m in pullable) or "(none ollama-backed in this selection)"
        rerun_args = [a for a in argv if a not in ("--yes", "-y")] + ["--yes"]
        output.fail(output.EXIT_CONFIRM,
                    f"pulling {len(pullable)} model(s) downloads gigabytes ({names})",
                    hint="re-run: plainkeep models pull " + " ".join(rerun_args), verb="models")
    if pullable and not shutil.which("ollama"):
        output.fail(output.EXIT_UNEXPECTED, "ollama not installed", hint="brew install ollama", verb="models")
    pulled = []
    for s, m in pullable:  # pragma: no cover - real download, never exercised in tests
        r = subprocess.run(["ollama", "pull", m], capture_output=True, text=True)
        pulled.append({"stage": s, "model": m, "ok": r.returncode == 0})
    data = {"pulled": pulled, "skipped": skipped}

    def render(d):
        lines = [f"  {p['stage']:<8} {p['model']:<24} {'ok' if p['ok'] else 'FAILED'}" for p in d["pulled"]]
        if d["skipped"]:
            lines.append(f"  skipped (not ollama-backed): {', '.join(d['skipped'])}")
        return "\n".join(lines) if lines else "nothing to pull"

    return output.emit(data, "models", human=render)


def _test_stt(input_path: str | None) -> str:  # pragma: no cover - real model call, never in tests
    if not input_path:
        raise ValueError("--input <audio file> required for stt")
    src = Path(input_path)
    if _have_mod("parakeet_mlx"):
        from parakeet_mlx import from_pretrained
        model = from_pretrained("mlx-community/parakeet-tdt-0.6b-v2")
        return "[parakeet-mlx] " + (model.transcribe(str(src)).text or "").strip()
    if _have_mod("mlx_whisper"):
        import mlx_whisper
        return "[mlx-whisper] " + (mlx_whisper.transcribe(str(src)).get("text") or "").strip()
    if _have_mod("faster_whisper"):
        from faster_whisper import WhisperModel
        segs, _ = WhisperModel("base").transcribe(str(src))
        return "[faster-whisper] " + "\n".join(s.text.strip() for s in segs)
    raise RuntimeError("no ASR backend installed — pip install parakeet-mlx (Apple Silicon) or faster-whisper")


def _run_stage_test(stage: str, model: str | None, input_path: str | None) -> str:  # pragma: no cover
    if stage == "stt":
        return _test_stt(input_path)
    if stage == "ocr":
        if not input_path:
            raise ValueError("--input <image> required for ocr")
        if model:
            os.environ["PLAINKEEP_OCR"] = model
        text, backend = imagelib.read_text(input_path)
        return f"[{backend}] {text}"
    if stage == "vlm":
        if not input_path:
            raise ValueError("--input <image> required for vlm")
        if model:
            os.environ["PLAINKEEP_VLM"] = model
        cap, desc, backend = imagelib.describe(input_path)
        return f"[{backend}] {cap}\n{desc}"
    if stage == "embed":
        vec = embed.embed_query("plainkeep models test", model=model)
        return f"[{model or embed.model_name()}] embedded a {len(vec)}-dim vector"
    if stage == "rerank":
        docs = ["plainkeep is a local-first personal operating system", "bananas are yellow"]
        scores = rerank.rerank("personal productivity system", docs)
        return f"[{rerank.backend()}] " + ", ".join(f"{d!r}={s:.3f}" for d, s in zip(docs, scores))
    if stage == "triage":
        ans = subprocess.run(["ollama", "run", model or agent.triage_model(),
                              "Classify as exactly one word, task or note: buy milk"],
                             capture_output=True, text=True, timeout=300).stdout.strip()
        return f"[{model or agent.triage_model()}] {ans}"
    # enrich
    if input_path:
        text = Path(input_path).read_text(encoding="utf-8", errors="replace")
    else:
        text = ("plainkeep is a local-first, git-versioned personal operating system: wiki notes, tasks, "
                "journal, and one `plainkeep <verb>` command surface across ~/plainkeep, ~/work, and ~/files.")
    result = enrichlib.enrich(text, model=model)
    return (f"[{result['backend']}] {result['description']}\n"
            f"keywords: {', '.join(result['keywords'])}")


def cmd_test(argv):
    pos = [a for a in argv if not a.startswith("-")]
    stage = pos[0] if pos else ""
    if stage not in STAGES:
        output.fail(output.EXIT_USAGE,
                    f"usage: plainkeep models test <stage> [--model <m>] [--input <file>] --yes  "
                    f"(stage one of: {', '.join(STAGES)})", verb="models")
    yes = ("--yes" in argv) or ("-y" in argv)
    model = argv[argv.index("--model") + 1] if "--model" in argv else None
    input_path = argv[argv.index("--input") + 1] if "--input" in argv else None
    if not yes:
        output.fail(output.EXIT_CONFIRM,
                    f"testing '{stage}' runs a real model (may pull gigabytes first)",
                    hint=f"re-run: plainkeep models test {stage} --yes", verb="models")
    t0 = time.time()
    try:
        out = _run_stage_test(stage, model, input_path)  # pragma: no cover - real model call
    except Exception as e:  # pragma: no cover
        output.fail(output.EXIT_UNEXPECTED, f"test failed: {e}", verb="models")
    data = {"stage": stage, "model": model, "output": out, "seconds": round(time.time() - t0, 2)}
    return output.emit(data, "models", human=lambda d: f"{d['stage']} ({d['seconds']}s):\n{d['output']}")


def main(argv):
    _, argv = output.parse_argv(argv)
    action = argv[0] if argv else ""
    rest = argv[1:]
    if action == "list":
        return cmd_list()
    if action == "status":
        return cmd_status()
    if action == "stop":
        return cmd_stop(rest)
    if action == "pull":
        return cmd_pull(rest)
    if action == "test":
        return cmd_test(rest)
    output.fail(output.EXIT_USAGE,
               "usage: plainkeep models list | status | stop [--all] [<model>] | "
               "pull [--stage <s>|--all] --yes | test <stage> [--model <m>] [--input <file>] --yes",
               verb="models")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
