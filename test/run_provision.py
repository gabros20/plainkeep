#!/usr/bin/env python3
"""
run_provision.py — PROVISIONING: the uv bootstrap, the delivered lock, and the frozen matrix
(ADR-020 / Phase 2 Task 4).

Three things are under test and they fail in different ways, so they are gated separately:

  4a  the uv BOOTSTRAP — pinned by version + sha256, installed inside the versioned engine tree,
      a system uv ignored, offline refusing with the exact manual command and leaving nothing behind.
  4b  the delivered LOCK — `pyproject.toml` + `uv.lock` ship with the code, their digests are
      recorded outside the sealed tree, and a tampered one fails its checksum rather than installing.
      Deliberately NOT gated on "byte-identical environments", which is not a credible claim: an
      installed environment holds platform-specific artifacts and absolute paths.
  4c  the frozen MATRIX — base is stdlib-only, `[search]` and `[models]` are exactly today's sets,
      the seven BYO imports are declared nowhere, and nothing that is not a Python distribution is in
      any of them.

WHAT THIS SUITE REFUSES TO DO: reach the network. Every cell here is offline, and the ones that need
a real `uv sync` run it OFFLINE against a wheel this file builds by hand (the shape `run_pluginsdk.py`
already uses for pip). A test that downloads from PyPI to prove provisioning works goes red when PyPI
has a bad afternoon, and a green run of it would have proved the network was up.

The one thing it cannot prove offline is the real download, which was measured by hand during the
task and recorded in its report: uv 0.12.1 fetched, sha256-verified, `uv sync --frozen` for `[search]`
(39 dists, lancedb 0.36.0 / fastembed 0.8.0 / pyarrow 25.0.0) and `[models]` (68 dists, no torch),
both importing. This suite proves the MECHANISM around that; the SUITE-NOTE at the end says so rather
than letting a green line imply more.

HERMETIC: every install goes to a temp PLAINKEEP_ENGINE_HOME; `~/.local/share/plainkeep` and
`~/.config/plainkeep` are neither read nor written.
"""
from __future__ import annotations
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from lib.hermetic import seal
seal()   # hermetic: an empty throwaway registry, never the developer's real vault

REPO = Path(__file__).resolve().parents[1]
# `lib` means bin/lib below this line — the same shuffle run_setup_layers.py performs, and for the
# same reason: an import is cached by NAME, so the test/lib packages the seal above pulled in have to
# leave sys.modules or `from lib import provision` is answered by test/lib.
sys.path = [str(REPO / "bin"),
            *[p for p in sys.path if Path(p or ".").resolve() != Path(__file__).resolve().parent]]
for _cached in [m for m in list(sys.modules) if m == "lib" or m.startswith("lib.")]:
    del sys.modules[_cached]
from lib import enginetree, provision      # noqa: E402  (after the seal + path setup, on purpose)

VERSION = (REPO / "VERSION").read_text(encoding="utf-8").strip()
PY = sys.executable
PROVISION_PY = REPO / "bin" / "lib" / "provision.py"
ENGINETREE_PY = REPO / "bin" / "lib" / "enginetree.py"
CORE = REPO / ".local" / "bin" / "plainkeep-core"
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
EXIT_OK, EXIT_UNEXPECTED, EXIT_USAGE, EXIT_CONFIRM, EXIT_NOT_FOUND, EXIT_DENY = 0, 1, 2, 3, 4, 5

results: list[tuple[str, bool, str]] = []
skipped: list[str] = []

# THE FROZEN TABLE, restated here as the ORACLE rather than read from the file under test. A test that
# reads pyproject.toml and asserts it equals pyproject.toml passes for any content at all; the plan
# section's table is the thing that must not change silently, so it is typed out.
FROZEN_SEARCH = [
    'lancedb>=0.25,<0.26 ; platform_system == "Darwin" and platform_machine == "x86_64"',
    'lancedb>=0.33 ; platform_system != "Darwin" or platform_machine != "x86_64"',
    "fastembed>=0.4",
]
FROZEN_MODELS = [
    "Pillow>=10.0",
    "trafilatura>=2.0",
    'mlx-vlm>=0.1 ; platform_system == "Darwin" and platform_machine == "arm64"',
]
# Declared NOWHERE, each behind a `find_spec` probe with a deterministic fallback. They must not
# silently join `[models]` — that extra would become a multi-GB torch download fired by a setup verb.
BYO_SEVEN = ["pymupdf4llm", "docling", "sentence_transformers", "sentence-transformers",
             "parakeet_mlx", "parakeet-mlx", "mlx_whisper", "mlx-whisper",
             "faster_whisper", "faster-whisper", "ocrmac"]
# Not Python distributions at all. uv provisions Python distributions; these stay explicit,
# confirm-gated setup actions.
NOT_PACKAGES = ["ollama", "restic", "tesseract", "launchd", "plainkeep-ui", "embeddinggemma"]


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# THE FIXTURE VAULT every subprocess in this suite points at, set once in main().
#
# WHY IT EXISTS, and it is the whole of the integration failure this suite shipped with. The cells
# below spawn the engine's own CLIs and the compiled core. They used to inherit `PLAINKEEP_HOME`
# unset — so each child ran the discovery chain against whatever the RUNNER's cwd happened to resolve
# to. In a git worktree that is nothing (`.plainkeep/` is gitignored, so the checkout carries no
# marker) and the cells passed; in the developer's REAL checkout, which is a marked and registered
# vault, discovery answered with the repository and the same cells went red — with refusals about the
# repo, not about anything the cells were testing.
#
# That is the defect ADR-017's cwd-dependence fix already named once: **the suite's verdict depended
# on the environment it happened to run in**. A data-only vault of the suite's own making removes the
# variable — it is the shape `init` produces, it is not any real vault, and nothing about the machine
# can change what the children see.
FIXTURE_HOME: Path | None = None


def base_env() -> dict:
    """The environment every child gets. `PLAINKEEP_HOME` is pinned; `PLAINKEEP_CONFIG_HOME` is
    already the sealed throwaway registry (`lib.hermetic.seal()`), so the real registry is neither
    read nor written."""
    return {"PLAINKEEP_HOME": str(FIXTURE_HOME)} if FIXTURE_HOME else {}


def make_fixture_vault(root: Path) -> Path:
    """A marked, DATA-ONLY vault — no `bin/`, no engine, nothing but the marker. Deliberately not a
    copy of the repo: the engine is a separate tree (ADR-017), and a vault that carries one is
    refused with exit 5, which is exactly the trap this helper exists to avoid."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".plainkeep").mkdir(exist_ok=True)
    (root / ".plainkeep" / "vault.json").write_text(
        json.dumps({"schema": "plainkeep.vault/1",
                    "id": "00000000-0000-4000-8000-00000000f1x0",
                    "created": "2026-08-02T00:00:00+00:00"}), encoding="utf-8")
    return root


def run(*argv: str, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True,
                          env={**os.environ, **base_env(), **(env or {})},
                          cwd=str(cwd) if cwd else None)


def prov(engine: Path, *args: str) -> subprocess.CompletedProcess:
    """The provisioning CLI, run against a specific engine tree (by running ITS copy of the module,
    which is how `ENGINE_ROOT = parents[2]` makes it answer for that tree)."""
    return run(PY, str(engine / "bin" / "lib" / "provision.py"), *args)


def install_engine(root: Path, src: Path | None = None) -> Path:
    r = run(PY, str(ENGINETREE_PY), "--install", str(src or REPO), "--force",
            env={"PLAINKEEP_ENGINE_HOME": str(root)})
    if r.returncode != 0:
        raise RuntimeError(f"fixture install failed: {r.stderr[:400]}")
    return root / "engine" / VERSION


def unseal_write(p: Path, text: str) -> None:
    """Edit a file inside a SEALED tree the way a hot-patcher would: chmod it writable, write, and
    chmod it back. This is the exact move `seal_problems` says it cannot catch, and it is how the
    digest cells below are made to exercise a region the seal check does not reach."""
    old = p.stat().st_mode
    p.chmod(0o644)
    p.write_text(text, encoding="utf-8")
    p.chmod(stat.S_IMODE(old))


def make_uv_tarball(dest: Path, target: str, body: str = "#!/bin/sh\necho fake-uv\n") -> str:
    """A tar.gz shaped like a uv release (`uv-<target>/uv`), for the local-artifact cells. Returns
    its sha256."""
    buf = io.BytesIO()
    payload = body.encode()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(f"uv-{target}/uv")
        info.size = len(payload)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(payload))
    dest.write_bytes(buf.getvalue())
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def repoint_pin(engine: Path, *, url: str, sha256: str | None = None, bless: bool = True) -> None:
    """Rewrite the engine's uv pin to point at a LOCAL artifact. `sha256=None` leaves the published
    digest in place, which is how the mismatch cell is built.

    `bless=True` RE-RECORDS the tree's digest manifest afterwards, which is the state "this is the pin
    the engine shipped with" — what the download-mechanism cells below need in order to be about the
    download rather than about the tamper gate. `bless=False` is the ATTACK, and it is now a refusal
    (`case_pin_is_gated`): this helper is the exploit primitive the r1 review found shipping as an
    unasserted fixture — a pin edit chooses both the URL and the sha256 the download is held to, so an
    unblessed one used to install and execute an attacker's binary."""
    p = engine / provision.PIN_REL
    pin = json.loads(p.read_text(encoding="utf-8"))
    pin["url_template"] = url
    if sha256 is not None:
        for t in pin["artifacts"]:
            pin["artifacts"][t] = sha256
    unseal_write(p, json.dumps(pin, indent=2) + "\n")
    if bless:
        enginetree.record_digests(engine)


# --- 4c: the frozen matrix ------------------------------------------------------------------------
def case_matrix() -> None:
    extras = provision.extras(REPO)
    check("4c base: the stdlib floor is a contract — [project].dependencies is EMPTY",
          provision.base_deps(REPO) == [], str(provision.base_deps(REPO)))
    check("4c [search]: exactly the frozen set, verbatim (markers included)",
          extras.get("search") == FROZEN_SEARCH, str(extras.get("search")))
    check("4c [models]: exactly the frozen set, verbatim (mlx-vlm marker included)",
          extras.get("models") == FROZEN_MODELS, str(extras.get("models")))
    check("4c: there are exactly two extras — a third would be an undeclared scope change",
          sorted(extras) == ["models", "search"], str(sorted(extras)))

    whole = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    declared = "\n".join(FROZEN_SEARCH + FROZEN_MODELS + provision.base_deps(REPO))
    for name in BYO_SEVEN:
        check(f"4c BYO: {name} is declared in no extra (it routes through the ADR-018 overlay)",
              name not in declared, declared[:200])
    for name in NOT_PACKAGES:
        check(f"4c: {name} is not a Python distribution and is in no extra",
              name not in declared.lower(), declared[:200])
    # Whitespace-normalised: the sentence wraps across comment lines, and a substring test against
    # the raw file is a test of where the line breaks fell.
    flat = " ".join(whole.replace("#", " ").split())
    check("4c: pyproject says out loud that setup models does two things and the extra covers one",
          "does two things and this extra covers one" in flat
          and "silently becomes a downloader" in flat, flat[:200])

    # The tiny TOML reader has a real oracle rather than a self-test.
    try:
        import tomllib
    except ImportError:
        skipped.append("tomllib cross-check (needs Python 3.11+; the reader exists because the "
                       "floor is 3.10)")
    else:
        ref = tomllib.loads(whole)["project"]
        check("4c: the stdlib-3.10 TOML reader agrees with tomllib on the extras",
              provision.extras(REPO) == ref["optional-dependencies"], str(provision.extras(REPO)))
        check("4c: the stdlib-3.10 TOML reader agrees with tomllib on the base dependencies",
              provision.base_deps(REPO) == ref["dependencies"])

    # The mirrors. They are allowed to exist; they are not allowed to disagree.
    reqs = (REPO / "requirements-search.txt").read_text(encoding="utf-8")
    check("4c: requirements-search.txt mirrors [search] spec-for-spec",
          all(s in reqs for s in FROZEN_SEARCH),
          [s for s in FROZEN_SEARCH if s not in reqs][:1])
    main_req = (REPO / "requirements.txt").read_text(encoding="utf-8")
    check("4c: requirements.txt carries every [search] spec",
          all(s in main_req for s in FROZEN_SEARCH))
    check("4c: requirements.txt carries the [models] packages",
          all(s.split(">=")[0].split(" ")[0] in main_req for s in FROZEN_MODELS))


def case_product_consults_the_matrix(tmp: Path) -> None:
    """THE CELL THAT MATTERS: does the PRODUCT read the file, or does it merely agree with it?

    Five times this phase a rule was added to a validated model, agreed, model-tested, and never
    called by anything shipped. So this does not compare `setuplib.search_deps()` to the frozen table
    (that would pass if setuplib still carried its own copy). It MUTATES a delivered engine's
    pyproject and then drives the real `plainkeep setup` verb, through a subprocess, and looks for
    the mutation in the command the verb says it would run."""
    root = tmp / "consult"
    engine = install_engine(root)
    vault = tmp / "consult-vault"
    (vault / ".plainkeep").mkdir(parents=True)
    # `lib.paths` refuses to import without a selected vault (Task 1b), and `setuplib` imports it at
    # module scope — so the fixture vault has to exist before the import, not after.
    os.environ["PLAINKEEP_HOME"] = str(vault)
    from lib import setuplib
    check("4c wiring: setuplib.search_deps() is the delivered [search] extra",
          setuplib.search_deps() == FROZEN_SEARCH, str(setuplib.search_deps()))
    check("4c wiring: setuplib.models_deps() is the delivered [models] extra",
          setuplib.models_deps() == FROZEN_MODELS, str(setuplib.models_deps()))
    (vault / ".plainkeep" / "vault.json").write_text(
        json.dumps({"schema": "plainkeep.vault/1",
                    "id": "00000000-0000-4000-8000-00000000c001",
                    "created": "2026-08-02T00:00:00+00:00"}), encoding="utf-8")
    env = {"PLAINKEEP_HOME": str(vault), "PLAINKEEP_SETUP_FAKE": "1",
           "PLAINKEEP_ASSUME_OLLAMA": "1", "PLAINKEEP_ENGINE": str(engine)}

    def setup_dry(layer: str) -> dict:
        r = run(PY, str(engine / "bin" / "setup" / "run.py"), layer, "--dry-run", "--json", env=env)
        for line in r.stdout.splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("data", obj).get("layer") == layer:
                return obj.get("data", obj)
        return {"_stderr": r.stderr, "_stdout": r.stdout}

    before = setup_dry("search")
    joined = " | ".join(before.get("ran", []))
    check("4c wiring: `setup search --dry-run` names the delivered lancedb specs",
          "lancedb>=0.33" in joined, joined[:300])
    check("4c wiring: `setup search` still installs NONE of the models packages",
          "Pillow" not in joined and "trafilatura" not in joined, joined[:300])

    # MUTATE the delivered project inside the sealed engine, then ask the verb again.
    #
    # The mutation is BLESSED (the manifest is re-recorded, exactly as `install()` would have if this
    # content had shipped) because the read is now GATED — `provision._pyproject_text` refuses on a
    # tree that does not match its checksums. Without the re-record this cell would be testing the
    # gate instead of the wiring, and the wiring is what it is for. The gate gets its own cell below,
    # which is the pair the r1 review asked for: the right assertion, with the gate in front of it.
    py_toml = engine / "pyproject.toml"
    mutated = py_toml.read_text(encoding="utf-8").replace(
        '    "fastembed>=0.4",\n', '    "fastembed>=0.4",\n    "sentinel-pkg==9.9.9",\n', 1)
    unseal_write(py_toml, mutated)
    enginetree.record_digests(engine)
    after = setup_dry("search")
    joined_after = " | ".join(after.get("ran", []))
    check("4c wiring: EDITING the delivered pyproject changes what `plainkeep setup search` runs "
          "(the product reads the file; it does not merely agree with it)",
          "sentinel-pkg==9.9.9" in joined_after, joined_after[:300])

    # THE GATE IN FRONT OF THAT SAME PROPERTY. An UNRECORDED edit — the hot patch, the one the seal
    # cannot see — must not reach a pip command line. Before the fix it did: `setuplib.search_deps()`
    # read the delivered project through `provision.extras()` with no digest check at all, so an
    # attacker-chosen package landed in a real `pip install` argv while `digest_problems` reported
    # the file as tampered. The gate lived only in `provision.sync()`, which no verb calls.
    unseal_write(py_toml, mutated.replace('    "sentinel-pkg==9.9.9",\n',
                                          '    "evil-attacker-package",\n', 1))
    check("4b GATE: the tamper is invisible to the seal and VISIBLE to the checksums",
          enginetree.seal_problems(engine) == []
          and any("pyproject.toml does not match" in p for p in enginetree.digest_problems(engine)),
          str(enginetree.digest_problems(engine))[:200])
    r = run(PY, str(engine / "bin" / "setup" / "run.py"), "search", "--dry-run", "--json", env=env)
    check("4b GATE: `plainkeep setup search` REFUSES on a hot-patched dependency matrix (exit 5), "
          "rather than putting an attacker-chosen package on a pip command line",
          r.returncode == EXIT_DENY, f"rc={r.returncode} {(r.stderr or r.stdout)[:250]}")
    check("4b GATE: the attacker's package never reaches an argv the operator would run",
          "evil-attacker-package" not in (r.stdout + r.stderr), (r.stdout + r.stderr)[:250])
    check("4b GATE: and it refuses as the protocol's error envelope, not as a traceback",
          "recorded checksums" in (r.stdout + r.stderr) and "Traceback" not in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[:250])
    # Back to a tree whose contents match its manifest, so the `models` cells below are about the
    # models layer rather than about the gate they have just proved.
    unseal_write(py_toml, mutated)
    enginetree.record_digests(engine)

    models = setup_dry("models")
    mjoined = " | ".join(models.get("ran", []))
    check("4c: `setup models` runs the WEIGHTS pull and the pip half as two separate steps",
          any("models" in c and "pull" in c for c in models.get("ran", []))
          and "Pillow" in mjoined, mjoined[:300])
    check("4c: no uv/pip step in the models layer downloads a model weight",
          not any(("pip" in c or "uv " in c) and ("ollama" in c or "pull" in c)
                  for c in models.get("ran", [])), mjoined[:300])
    check("4c: the models layer's --json payload states which half is which",
          models.get("halves") and "GIGABYTES" in models["halves"][0]
          and "extra" in models["halves"][1], str(models.get("halves"))[:200])

    r = run(PY, str(engine / "bin" / "setup" / "run.py"), "models", env=env)
    check("4c: confirming `setup models` names BOTH halves before the operator says yes",
          r.returncode == EXIT_CONFIRM and "TWO things" in r.stderr
          and "GIGABYTES" in r.stderr and "[models] extra" in r.stderr,
          (r.stderr or r.stdout)[:300])


# --- 4a: the pin and the bootstrap ------------------------------------------------------------------
def case_pin() -> None:
    pin = provision.load_pin(REPO)
    check("4a pin: the engine records an exact uv version",
          isinstance(pin["version"], str) and pin["version"].count(".") == 2, str(pin.get("version")))
    check("4a pin: every pinned target carries a 64-hex sha256",
          all(len(d) == 64 and all(c in "0123456789abcdef" for c in d)
              for d in pin["artifacts"].values()))
    check("4a pin: both macOS and both glibc/musl Linux arches are pinned",
          {"aarch64-apple-darwin", "x86_64-apple-darwin", "x86_64-unknown-linux-gnu",
           "aarch64-unknown-linux-gnu"} <= set(pin["artifacts"]), str(sorted(pin["artifacts"])))
    check("4a pin: this host's target is one of them",
          provision.platform_target() in pin["artifacts"], provision.platform_target())

    bad = dict(pin, artifacts={"x": "not-a-digest"})
    tmpdir = Path(tempfile.mkdtemp())
    (tmpdir / "bin" / "lib").mkdir(parents=True)
    (tmpdir / "bin" / "lib" / "uvpin.json").write_text(json.dumps(bad), encoding="utf-8")
    try:
        provision.load_pin(tmpdir)
        check("4a pin: a malformed digest in our OWN pin refuses as a pin error", False,
              "load_pin accepted it")
    except Exception as e:
        check("4a pin: a malformed digest in our OWN pin refuses as a pin error "
              "(not later, as a phantom tamper report)", "malformed sha256" in str(e), str(e))
    shutil.rmtree(tmpdir, ignore_errors=True)

    try:
        provision.artifact(pin, "sparc64-unknown-linux-gnu")
        check("4a pin: an unpinned platform refuses BY NAME rather than guessing a near target",
              False, "artifact() returned something")
    except Exception as e:
        # The list of pinned targets rides in the HINT, which is where a refusal puts the operator's
        # next move (output.fail prints both).
        both = f"{e} {getattr(e, 'hint', '')}"
        check("4a pin: an unpinned platform refuses BY NAME rather than guessing a near target, and "
              "names the targets that ARE pinned",
              "not pinned for sparc64" in both and "pinned targets" in both, both)


def case_offline_refusal(tmp: Path) -> None:
    engine = install_engine(tmp / "offline")
    pin = provision.load_pin(engine)
    _, url, digest, member = provision.artifact(pin)
    text = provision.offline_refusal(engine, pin)
    dest = provision.uv_path(engine, pin)
    check("4a offline: the refusal carries the download URL", url in text)
    check("4a offline: the refusal carries the expected sha256", digest in text)
    check("4a offline: the refusal carries the destination path", str(dest) in text)
    check("4a offline: the refusal is a runnable two-step, not a diagnosis",
          "curl -fsSL" in text and "shasum -a 256 -c -" in text and "chmod 555" in text, text[:200])

    r = prov(engine, "--ensure-uv", "--offline")
    check("4a offline: `--ensure-uv --offline` REFUSES rather than falling back to pip or to PATH",
          r.returncode != 0, f"rc={r.returncode}")
    check("4a offline: the refusal an operator actually sees carries all three facts",
          url in r.stderr and digest in r.stderr and str(dest) in r.stderr, r.stderr[:300])
    leftovers = list((engine / enginetree.PROVISION_DIR).glob(".incoming-uv-*"))
    check("4a offline: NO PARTIAL PROVISIONING is left behind after the refusal",
          leftovers == [] and not dest.exists(), str(leftovers))


def case_system_uv_is_ignored(tmp: Path) -> None:
    """A uv on PATH is not preferred, not fallen back to, and not consulted."""
    engine = install_engine(tmp / "sysuv")
    fakebin = tmp / "fakebin"
    fakebin.mkdir()
    marker = tmp / "system-uv-was-run"
    (fakebin / "uv").write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    (fakebin / "uv").chmod(0o755)
    env = {"PATH": f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}"}

    r = run(PY, str(engine / "bin" / "lib" / "provision.py"), "--print", "uv", env=env)
    check("4a: the uv the engine will run is inside the ENGINE, never the one on PATH",
          str(engine / enginetree.PROVISION_DIR) in r.stdout, r.stdout.strip()[:200])
    r = run(PY, str(engine / "bin" / "lib" / "provision.py"), "--ensure-uv", "--offline", env=env)
    check("4a: with a uv sitting on PATH, an offline bootstrap still REFUSES (the pin is the point)",
          r.returncode != 0 and "cannot" in (r.stderr or ""), r.stderr[:200])
    check("4a: the uv on PATH was never executed", not marker.exists())
    r = run(PY, str(engine / "bin" / "lib" / "provision.py"), "--print", "system-uv", env=env)
    check("4a: a system uv is REPORTED (one line, for doctor) and nothing more",
          r.stdout.strip() == str(fakebin / "uv"), r.stdout.strip()[:200])


def case_checksum_gate(tmp: Path) -> None:
    """The pin verified against a real download — locally served, so it runs offline."""
    engine = install_engine(tmp / "checksum")
    target = provision.platform_target()
    art_dir = tmp / "artifacts"
    art_dir.mkdir(exist_ok=True)
    good_tar = art_dir / f"uv-{target}.tar.gz"
    good_sha = make_uv_tarball(good_tar, target)
    url_tpl = f"file://{art_dir}/uv-{{target}}.tar.gz"

    # (1) MISMATCH: the pin keeps the published digest, the artifact is ours. Must refuse.
    repoint_pin(engine, url=url_tpl)
    r = prov(engine, "--ensure-uv")
    dest = provision.uv_path(engine, provision.load_pin(engine))
    check("4a checksum: a download that does not match the pin REFUSES",
          r.returncode != 0 and "does not match its pinned sha256" in r.stderr, r.stderr[:300])
    check("4a checksum: the refusal shows expected vs got",
          "expected" in r.stderr and "got" in r.stderr, r.stderr[:300])
    check("4a checksum: nothing was installed and nothing was left half-installed",
          not dest.exists() and not list((engine / enginetree.PROVISION_DIR).glob(".incoming-uv-*")),
          str(list((engine / enginetree.PROVISION_DIR).glob("*"))))

    # (2) MATCH: the same mechanism, with the pin telling the truth. Must install, verified and sealed.
    repoint_pin(engine, url=url_tpl, sha256=good_sha)
    r = prov(engine, "--ensure-uv")
    check("4a checksum: a download that MATCHES the pin installs", r.returncode == 0, r.stderr[:300])
    check("4a: uv lands inside the VERSIONED engine tree, so it rolls back with the engine",
          dest.exists() and dest.is_relative_to(engine), str(dest))
    mode = dest.stat().st_mode
    check("4a: the verified binary is sealed read-only (0555) and executable",
          stat.S_IMODE(mode) == 0o555, oct(stat.S_IMODE(mode)))
    check("4a: its version directory is sealed too",
          stat.S_IMODE(dest.parent.stat().st_mode) == 0o555,
          oct(stat.S_IMODE(dest.parent.stat().st_mode)))

    # (3) IDEMPOTENT BY CONTENT: a truncated binary is replaced, not trusted.
    dest.parent.chmod(0o755)
    dest.chmod(0o644)
    dest.write_text("truncated\n", encoding="utf-8")
    dest.chmod(0o555)
    dest.parent.chmod(0o555)
    r = prov(engine, "--ensure-uv", "--offline")
    check("4a: an EXISTING uv that no longer matches the pin is not trusted — it is re-hashed, "
          "removed, and (offline) refused rather than run",
          r.returncode != 0 and not dest.exists(), f"rc={r.returncode} exists={dest.exists()}")


# --- 4a: the PIN is inside the gate ---------------------------------------------------------------
def case_pin_is_gated(tmp: Path) -> None:
    """THE PIN IS A GATED FILE, on every entry point that can download or execute.

    The r1 review reproduced this end to end: `bin/lib/uvpin.json` is in the digest manifest, but the
    gate named `(pyproject.toml, uv.lock)` by hand and never asked about it. Because the pin supplies
    BOTH the download URL and the sha256 the download is verified against, tampering with it is
    self-consistent — "verify before making it executable" verified the attacker's bytes against the
    attacker's number. A payload was installed, sealed 0555 and EXECUTED TWICE by `--sync`, exit 0, on
    both implementations; `--ensure-uv` installed it with no gate at all.

    So this cell tampers the pin the way a hot-patcher would (chmod → edit → chmod back, the move the
    seal check admits it cannot see) and asserts the refusal: exit 5, nothing under `tools/`, and the
    payload NEVER RUN. The marker file is the strongest available assertion — a fake uv that records
    every invocation cannot record one that did not happen.

    A FRESH ENGINE AND A FRESH MARKER PER VERB, deliberately. Looping both verbs over one tree makes
    the second verb's cells pass on the FIRST verb's refusal: `sync` gates, leaves `tools/` empty, and
    `ensure-uv` then has nothing to find. Each verb has its own gate to prove — `--ensure-uv` is
    reachable without `sync` and is the command whose whole job is to install and seal an executable —
    so each gets a tree nobody has refused on its behalf."""
    target = provision.platform_target()
    art_dir = tmp / "pwn-artifacts"
    art_dir.mkdir(exist_ok=True)

    def gated(label: str, argv_for) -> None:
        if argv_for(install_engine(tmp / f"pingate-{label}-probe"), "sync") is None:
            skipped.append(f"the PIN-gate cells on the {label} path (the installed engine carries "
                           "no core binary)")
            return
        for verb in ("sync", "ensure-uv"):
            engine = install_engine(tmp / f"pingate-{label}-{verb}")
            marker = tmp / f"payload-executed-{label}-{verb}.txt"
            sha = make_uv_tarball(
                art_dir / f"{label}-{verb}-uv-{target}.tar.gz", target,
                body=f'#!/bin/sh\necho "PWNED args=$*" >> {marker}\nexit 0\n')
            # The attacker's pin: our URL, our digest, and the recorded checksums NOT updated.
            repoint_pin(engine, url=f"file://{art_dir}/{label}-{verb}-uv-{{target}}.tar.gz",
                        sha256=sha, bless=False)
            check(f"4a PIN GATE ({label} {verb}): the tamper is invisible to the SEAL, which is why "
                  "a checksum gate has to be the thing that sees it",
                  enginetree.seal_problems(engine) == [], str(enginetree.seal_problems(engine)))
            check(f"4a PIN GATE ({label} {verb}): the digest manifest DOES record the pin — the "
                  "evidence exists, and before the fix nothing consulted it",
                  any("uvpin.json" in p for p in enginetree.digest_problems(engine)),
                  str(enginetree.digest_problems(engine)))
            dest = provision.uv_path(engine, provision.load_pin(engine))
            r = run(*argv_for(engine, verb))
            check(f"4a PIN GATE ({label} {verb}): a tampered pin REFUSES with exit 5",
                  r.returncode == EXIT_DENY, f"rc={r.returncode} {(r.stderr or r.stdout)[:200]}")
            check(f"4a PIN GATE ({label} {verb}): and it names the file, so the refusal is actionable",
                  "uvpin.json does not match its recorded checksum" in r.stderr, r.stderr[:250])
            check(f"4a PIN GATE ({label} {verb}): NOTHING was installed under tools/",
                  not dest.exists()
                  and not list((engine / enginetree.PROVISION_DIR).glob(".incoming-uv-*")),
                  str(sorted(p.name for p in (engine / enginetree.PROVISION_DIR).glob("*"))))
            check(f"4a PIN GATE ({label} {verb}): the attacker's binary was NEVER EXECUTED",
                  not marker.exists(),
                  marker.read_text(encoding="utf-8")[:200] if marker.exists() else "")

    def py_argv(engine: Path, verb: str) -> list[str]:
        return [PY, str(engine / "bin" / "lib" / "provision.py"),
                "--sync" if verb == "sync" else "--ensure-uv"]

    def core_argv(engine: Path, verb: str) -> list[str] | None:
        core = engine / ".local" / "bin" / "plainkeep-core"
        return [str(core), "--core-provision", verb] if core.is_file() else None

    gated("python", py_argv)
    # The core path is the one that matters MOST here: on a machine with no system python3 it is the
    # only way to provision, so a gate only the Python side enforced would hold exactly where it is
    # not needed. Same currency guard as `case_core_parity` — a stale binary skips loudly.
    if not CORE.is_file() or not core_speaks_provision(CORE):
        skipped.append("the PIN-gate cells on the CORE path (" + (
            "no compiled plainkeep-core — build it: cd cli && bun run build"
            if not CORE.is_file() else STALE_CORE) + ")")
        return
    gated("core", core_argv)


def case_injected_file_is_refused(tmp: Path) -> None:
    """A file ADDED to the engine tree is a tamper, and both implementations have to say so.

    Checking only the RECORDED paths answers "was anything changed"; an attacker who ADDS
    `bin/lib/sitecustomize.py` changes nothing recorded. Python's `digest_problems` has always
    reported these when it checks the whole tree (`is present but was never recorded`) and the TS
    port did not — invisible while the gate named two files by hand, since neither can be extra, and
    live the moment the gate widened to the tree. The core is the only provisioning path on a machine
    with no system python3, so a check that holds only on the Python side holds where it is not
    needed.

    One injection PER OWNED TREE, which is what makes this a statement about the WALK rather than
    about one lucky path: a port that forgot `skills/` or `templates/` passes a single-file version of
    this cell and fails here."""
    engine = install_engine(tmp / "injected")
    injected = []
    for tree in ("bin/lib", "templates/verb", "frontends/raycast", "skills/operate-plainkeep"):
        d = engine / tree
        if not d.is_dir():
            continue
        old = stat.S_IMODE(d.stat().st_mode)
        d.chmod(0o755)
        (d / "pk_injected.py").write_text("# attacker\n", encoding="utf-8")
        d.chmod(old)
        injected.append(f"{tree}/pk_injected.py")
    check("4b INJECTION: the added files are invisible to the SEAL (they arrive 0644 in a 0555 tree, "
          "and the seal check asks about modes, not about membership)",
          enginetree.seal_problems(engine) == [], str(enginetree.seal_problems(engine))[:200])
    problems = enginetree.digest_problems(engine)
    check("4b INJECTION: the Python side reports EVERY injected file as never recorded",
          all(any(rel in p and "never recorded" in p for p in problems) for rel in injected),
          f"injected={injected} problems={problems}")
    r = prov(engine, "--sync")
    check("4b INJECTION: `provision.py --sync` REFUSES an engine carrying files nobody installed",
          r.returncode == EXIT_DENY, f"rc={r.returncode} {(r.stderr or r.stdout)[:200]}")
    core = engine / ".local" / "bin" / "plainkeep-core"
    if not (CORE.is_file() and core_speaks_provision(CORE) and core.is_file()):
        skipped.append("the INJECTION cells on the CORE path (" + (
            "no compiled plainkeep-core — build it: cd cli && bun run build"
            if not CORE.is_file() else STALE_CORE) + ")")
        return
    c = run(str(core), "--core-provision", "sync")
    check("4b INJECTION: and the CORE reaches the same verdict — the walk covers the same trees, so "
          "the two implementations agree on what `intact` means",
          c.returncode == EXIT_DENY, f"rc={c.returncode} {(c.stderr or c.stdout)[:200]}")
    check("4b INJECTION: the core names every injected file, in every owned tree it had to walk to "
          "find them",
          all(rel in c.stderr for rel in injected), f"injected={injected} stderr={c.stderr[:400]}")


# --- 4a: where this lands relative to the seal --------------------------------------------------------
def case_seal_interaction(tmp: Path) -> None:
    engine = install_engine(tmp / "seal")
    tools = engine / enginetree.PROVISION_DIR
    check("4a seal: an installed engine HAS a tools/ directory", tools.is_dir())
    check("4a seal: tools/ is the ONE writable path — 0755 in an otherwise 0555 tree",
          stat.S_IMODE(tools.stat().st_mode) == 0o755, oct(stat.S_IMODE(tools.stat().st_mode)))
    check("4a seal: the engine ROOT is still sealed",
          stat.S_IMODE(engine.stat().st_mode) == 0o555, oct(stat.S_IMODE(engine.stat().st_mode)))
    for rel in ("bin", "bin/lib", "bin/lib/enginetree.py", "plainkeep", "pyproject.toml", "uv.lock"):
        m = stat.S_IMODE((engine / rel).stat().st_mode)
        check(f"4a seal: {rel} is still read-only ({oct(m)})", not m & stat.S_IWUSR, oct(m))
    check("4a seal: a sealed tree with a writable tools/ verifies clean",
          enginetree.verify(engine) == [], str(enginetree.verify(engine)))

    # The INVERSE check is in the model, not an accident of what the walk misses.
    tools.chmod(0o555)
    problems = enginetree.verify(engine)
    check("4a seal: a tools/ that got sealed by mistake is REPORTED (provisioning would fail at the "
          "last step, silently, without this)",
          any("tools/ is read-only" in p for p in problems), str(problems))
    tools.chmod(0o755)
    # A CHECKOUT is not required to carry tools/ — it is created on first provisioning — so the
    # absence check is scoped to installed trees. Proved in both directions rather than asserted:
    check("4a seal: a CHECKOUT with no tools/ still verifies clean (it is created on first use)",
          enginetree.verify(REPO, check_seal=False) == []
          or all("tools" not in p for p in enginetree.verify(REPO, check_seal=False)),
          str(enginetree.verify(REPO, check_seal=False))[:200])
    # Removing it needs the ROOT unsealed — an unlink is a write to the containing directory. Worth
    # noting rather than working around silently: the seal makes `tools/` un-DELETABLE while leaving
    # it writable, which is the shape wanted (provisioning may fill it; nothing may remove it).
    engine.chmod(0o755)
    shutil.rmtree(tools)
    engine.chmod(0o555)
    check("4a seal: a MISSING tools/ is reported as a missing engine tree",
          any("missing engine tree: tools/" in p for p in enginetree.verify(engine)),
          str(enginetree.verify(engine)))


def case_reseal_keeps_the_environment(tmp: Path) -> None:
    """RE-SEALING A PROVISIONED TREE MUST NOT SEAL THE ENVIRONMENT INSIDE IT.

    `_chmod_tree`'s walk is `root.rglob("*")` — the whole tree — and `_seal_installed` re-opened
    `tools/` ITSELF only, so a tree that had already been provisioned came out of a re-seal with
    `tools/venv` and the uv-managed interpreter under it read-only. `verify()` asks whether the one
    directory is writable, which it was, so the half-sealed state was invisible and the next
    `uv sync` failed on permissions inside a venv uv owns.

    Driven through the PRODUCT, on the documented shape that reaches it: `--install --writable` (a dev
    install) → provision → plain `--install`, which takes the repair branch because `verify()` returns
    `[_UNSEALED]`. The assertion is about MODES, not about `verify()`'s verdict — `verify()` said OK
    throughout, which is the half of the defect that made it survive."""
    home = tmp / "reseal"
    r = run(PY, str(ENGINETREE_PY), "--install", str(REPO), "--force", "--writable",
            env={"PLAINKEEP_ENGINE_HOME": str(home)})
    if r.returncode != 0:
        raise RuntimeError(f"fixture install failed: {r.stderr[:400]}")
    engine = home / "engine" / VERSION
    # What `uv sync` leaves behind: an environment uv owns, and an interpreter inside it.
    venvbin = engine / enginetree.PROVISION_DIR / provision.VENV_DIRNAME / "bin"
    venvbin.mkdir(parents=True)
    (venvbin / "python3").write_text("#!/bin/sh\nexec true\n", encoding="utf-8")
    (venvbin / "python3").chmod(0o755)
    site = engine / enginetree.PROVISION_DIR / provision.VENV_DIRNAME / "lib" / "python3.99" / "sp"
    site.mkdir(parents=True)
    (site / "installed.py").write_text("X = 1\n", encoding="utf-8")

    r = run(PY, str(ENGINETREE_PY), "--install", str(REPO), env={"PLAINKEEP_ENGINE_HOME": str(home)})
    check("4a seal: a plain --install over an unsealed tree takes the REPAIR branch and seals it",
          r.returncode == 0 and not stat.S_IMODE(engine.stat().st_mode) & stat.S_IWUSR,
          f"rc={r.returncode} {oct(stat.S_IMODE(engine.stat().st_mode))} {r.stderr[:150]}")
    for rel in ("bin/lib/provision.py", "pyproject.toml"):
        m = stat.S_IMODE((engine / rel).stat().st_mode)
        check(f"4a seal: the ENGINE CODE is sealed by that repair ({rel})", not m & stat.S_IWUSR,
              oct(m))
    for rel in (f"{enginetree.PROVISION_DIR}/{provision.VENV_DIRNAME}",
                f"{enginetree.PROVISION_DIR}/{provision.VENV_DIRNAME}/bin",
                f"{enginetree.PROVISION_DIR}/{provision.VENV_DIRNAME}/bin/python3",
                f"{enginetree.PROVISION_DIR}/{provision.VENV_DIRNAME}/lib/python3.99/sp/installed.py"):
        m = stat.S_IMODE((engine / rel).stat().st_mode)
        check(f"4a seal: the PROVISIONED environment survives the re-seal writable ({rel}) — uv owns "
              "it, and a sealed venv is a `uv sync` that fails on permissions",
              m & stat.S_IWUSR, oct(m))
    check("4a seal: and the tree still verifies clean (it did BEFORE the fix too — the mode check "
          "cannot see inside tools/, which is why this cell asserts modes and not the verdict)",
          enginetree.verify(engine) == [], str(enginetree.verify(engine)))
    # The other direction is unchanged and load-bearing: unsealing must still reach into tools/, or
    # `remove_version` cannot delete a provisioned tree.
    (venvbin / "python3").chmod(0o555)
    venvbin.chmod(0o555)
    enginetree._chmod_tree(engine, writable=True)
    check("4a seal: UNSEALING still walks tools/ — it is only ever a prelude to rmtree, which has to "
          "reach everything",
          stat.S_IMODE(venvbin.stat().st_mode) & stat.S_IWUSR, oct(stat.S_IMODE(venvbin.stat().st_mode)))


# --- 4b: the delivered lock and its checksums ---------------------------------------------------------
def case_delivered_lock(tmp: Path) -> None:
    engine = install_engine(tmp / "lock")
    for rel in ("pyproject.toml", "uv.lock"):
        check(f"4b: {rel} SHIPS INSIDE the engine tree (one artifact with the code)",
              (engine / rel).is_file(), str(engine / rel))
        check(f"4b: {rel} is in the ownership manifest, so an install without it is refused",
              rel in enginetree.OWNED_FILES, str(enginetree.OWNED_FILES))
    check("4b: the delivered lock is the SAME BYTES as the source checkout's",
          (engine / "uv.lock").read_bytes() == (REPO / "uv.lock").read_bytes())

    dpath = enginetree.digests_path(engine)
    check("4b: the checksum manifest is recorded OUTSIDE the tree it covers "
          "(an edit inside a sealed tree cannot rewrite it)",
          dpath.is_file() and not dpath.is_relative_to(engine), str(dpath))
    recorded = enginetree.read_digests(engine) or {}
    check("4b: the manifest covers pyproject.toml and uv.lock",
          "pyproject.toml" in recorded and "uv.lock" in recorded, str(sorted(recorded))[:200])
    check("4b: the manifest covers the whole owned set, not a sample",
          len(recorded) > 100, str(len(recorded)))
    check("4b: a freshly installed tree matches its recorded checksums",
          enginetree.digest_problems(engine) == [], str(enginetree.digest_problems(engine))[:300])

    # THE TAMPER. Done exactly the way the seal check admits it cannot catch: chmod, edit, chmod back.
    lock = engine / "uv.lock"
    unseal_write(lock, (lock.read_text(encoding="utf-8")
                        .replace('name = "fastembed"', 'name = "fastembed-evil"', 1)))
    check("4b: the SEAL still says the tree is fine after a hot patch that put the mode back "
          "(this is the gap the checksums exist to close)",
          enginetree.seal_problems(engine) == [], str(enginetree.seal_problems(engine)))
    problems = enginetree.digest_problems(engine)
    check("4b: the CHECKSUMS catch it, and name the file",
          any("uv.lock does not match" in p for p in problems), str(problems)[:300])
    check("4b: the narrow check `sync` uses catches it too",
          enginetree.digest_problems(engine, only=("pyproject.toml", "uv.lock")) != [])

    # A TAMPERED LOCK FAILS ITS CHECKSUM RATHER THAN INSTALLING — before uv is reached at all.
    r = prov(engine, "--sync", "--offline")
    check("4b GATE: a tampered lock REFUSES rather than provisioning from it",
          r.returncode == EXIT_DENY, f"rc={r.returncode} {r.stderr[:200]}")
    check("4b GATE: it refuses on the CHECKSUM, before uv is downloaded or run "
          "(the offline refusal would be the other outcome, and is not the one that fires)",
          "recorded checksums" in r.stderr and "cannot download uv" not in r.stderr, r.stderr[:300])

    # And a tree that is intact gets past the checksum gate to the (offline) uv step.
    engine2 = install_engine(tmp / "lock-ok")
    r = prov(engine2, "--sync", "--offline")
    check("4b: an INTACT tree passes the checksum gate and stops at the pinned-uv step instead",
          "recorded checksums" not in r.stderr and "uv 0" in r.stderr, r.stderr[:300])

    enginetree_versions = tmp / "lock" / "engine"
    run(PY, str(ENGINETREE_PY), "--install", str(REPO), "--force", "--version", "9.9.9-tmp",
        env={"PLAINKEEP_ENGINE_HOME": str(tmp / "lock")})
    tmp_engine = enginetree_versions / "9.9.9-tmp"
    check("4b: a second version gets its own manifest",
          enginetree.digests_path(tmp_engine).is_file())
    old_home = os.environ.get("PLAINKEEP_ENGINE_HOME")
    os.environ["PLAINKEEP_ENGINE_HOME"] = str(tmp / "lock")
    try:
        enginetree.remove_version("9.9.9-tmp")
    finally:
        if old_home is None:
            os.environ.pop("PLAINKEEP_ENGINE_HOME", None)
        else:
            os.environ["PLAINKEEP_ENGINE_HOME"] = old_home
    check("4b: removing a version removes its manifest with it",
          not enginetree.digests_path(tmp_engine).is_file() and not tmp_engine.is_dir(),
          str(enginetree.digests_path(tmp_engine)))


# --- 4b: uv sync --frozen, for real, offline ----------------------------------------------------------
def _local_uv() -> Path | None:
    """The uv this CHECKOUT has provisioned, if it has. Never one from PATH — same rule as the
    product's. Absent means the uv-driven cells skip, loudly."""
    try:
        p = provision.uv_path(REPO)
    except Exception:
        return None
    return p if os.access(str(p), os.X_OK) else None


def make_wheel(dest_dir: Path, name: str, version: str, module: str) -> Path:
    """A minimal, valid wheel, built by hand — so the sync cells below need no network at all."""
    import zipfile
    dist = f"{name.replace('-', '_')}-{version}"
    whl = dest_dir / f"{dist}-py3-none-any.whl"
    record = []
    with zipfile.ZipFile(whl, "w") as z:
        def add(arcname: str, data: str) -> None:
            z.writestr(arcname, data)
            record.append(arcname)
        add(f"{module}.py", f"VALUE = {version!r}\n")
        add(f"{dist}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        add(f"{dist}.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: plainkeep-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        z.writestr(f"{dist}.dist-info/RECORD",
                   "".join(f"{r},,\n" for r in record) + f"{dist}.dist-info/RECORD,,\n")
    return whl


def case_frozen_sync_offline(tmp: Path) -> None:
    uv = _local_uv()
    if uv is None:
        skipped.append("uv sync --frozen cells (this checkout has not provisioned uv — run "
                       "`python3 bin/lib/provision.py --ensure-uv` once)")
        return
    work = tmp / "frozen"
    finds = work / "wheels"
    finds.mkdir(parents=True)
    make_wheel(finds, "pk-sample", "1.0.0", "pk_sample")
    make_wheel(finds, "pk-sample", "2.0.0", "pk_sample")
    (work / "pyproject.toml").write_text(
        '[project]\nname = "pk-frozen-fixture"\nversion = "0"\nrequires-python = ">=3.10"\n'
        'dependencies = ["pk-sample==1.0.0"]\n\n[tool.uv]\npackage = false\n'
        f'[tool.uv.sources]\n\n[[tool.uv.index]]\nname = "local"\nurl = "{finds.as_uri()}"\n'
        "explicit = false\n", encoding="utf-8")
    env = {**os.environ, "UV_NO_CONFIG": "1", "UV_OFFLINE": "1",
           "UV_PROJECT_ENVIRONMENT": str(work / "venv"),
           "UV_FIND_LINKS": str(finds), "UV_NO_INDEX": "1",
           "UV_PYTHON": sys.executable}
    r = subprocess.run([str(uv), "lock", "--no-config", "--project", str(work), "--offline"],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        skipped.append(f"uv sync --frozen cells (offline lock against hand-built wheels failed: "
                       f"{(r.stderr or '').strip().splitlines()[-1:]})")
        return
    lock_text = (work / "uv.lock").read_text(encoding="utf-8")
    check("4b sync: the lock names the EXACT version resolved",
          'name = "pk-sample"' in lock_text and 'version = "1.0.0"' in lock_text,
          lock_text[:200])
    # A LOCAL find-links registry records a `path` rather than a `hash` (measured), so the
    # "and hashes" half of the gate is asserted where it actually applies: the DELIVERED lock, which
    # was resolved against a real index.
    delivered = (REPO / "uv.lock").read_text(encoding="utf-8")
    check("4b sync GATE: the DELIVERED lock names a sha256 for every artifact it resolves",
          delivered.count('hash = "sha256:') > 100, str(delivered.count('hash = "sha256:')))
    check("4b sync GATE: the DELIVERED lock names exact versions, per platform marker",
          'name = "lancedb"' in delivered and 'name = "fastembed"' in delivered
          and "platform_machine" in delivered)
    check("4b sync: a local-registry lock names the resolved artifact by path",
          "pk_sample-1.0.0-py3-none-any.whl" in lock_text, lock_text[:200])

    r = subprocess.run([str(uv), "sync", "--frozen", "--no-config", "--project", str(work),
                        "--offline"], capture_output=True, text=True, env=env)
    check("4b sync: `uv sync --frozen` provisions from the DELIVERED lock, offline",
          r.returncode == 0, (r.stderr or "")[-300:])
    vpy = work / "venv" / "bin" / "python3"
    r2 = subprocess.run([str(vpy), "-c", "import pk_sample; print(pk_sample.VALUE)"],
                        capture_output=True, text=True)
    check("4b sync GATE: the resulting environment IMPORTS every declared dependency at the exact "
          "version the lock names (not 'byte-identical', which no installed environment can be)",
          r2.returncode == 0 and r2.stdout.strip() == "1.0.0", (r2.stdout + r2.stderr)[:200])

    dists = sorted(p.name for p in (work / "venv" / "lib").glob("python*/site-packages/*.dist-info"))
    check("4b sync GATE: NOTHING was installed beyond what the lock names — no weights, no system "
          "packages, no incidental extras",
          dists == ["pk_sample-1.0.0.dist-info"], str(dists))

    # --frozen is the load-bearing flag: a lock that disagrees with the project must REFUSE rather
    # than silently re-resolve (which, in a sealed engine, would fail at the write instead).
    (work / "pyproject.toml").write_text(
        (work / "pyproject.toml").read_text(encoding="utf-8")
        .replace('"pk-sample==1.0.0"', '"pk-sample==2.0.0"'), encoding="utf-8")
    r = subprocess.run([str(uv), "sync", "--frozen", "--no-config", "--project", str(work),
                        "--offline"], capture_output=True, text=True, env=env)
    check("4b sync: `--frozen` does NOT notice a lock that stopped matching the project — it "
          "installs the stale resolution and exits 0. Measured, not assumed; this is why the "
          "product runs a separate check first",
          r.returncode == 0, f"rc={r.returncode} {(r.stdout + r.stderr)[-200:]}")
    r = subprocess.run([str(uv), "lock", "--check", "--no-config", "--project", str(work),
                        "--offline"], capture_output=True, text=True, env=env)
    check("4b sync GATE: `uv lock --check` — the preflight `provision.sync()` runs — REFUSES that "
          "same stale pair, offline",
          r.returncode != 0 and "needs to be updated" in (r.stdout + r.stderr),
          f"rc={r.returncode} {(r.stdout + r.stderr)[-200:]}")
    check("4b sync: and the preflight is the command the product would actually run",
          provision.check_argv(REPO, uv=uv)[1:] == ["lock", "--check", "--no-config",
                                                    "--project", str(REPO)],
          str(provision.check_argv(REPO, uv=uv)))

    check("4b sync: the argv the product would run carries --frozen and the delivered project",
          provision.sync_argv(REPO, extras_wanted=("search",), uv=uv)[1:]
          == ["sync", "--frozen", "--no-config", "--project", str(REPO), "--extra", "search"],
          str(provision.sync_argv(REPO, extras_wanted=("search",), uv=uv)))
    senv = provision.sync_env(REPO, offline=True)
    check("4b sync: uv runs with the operator's own uv config DISABLED (the pin is the point)",
          senv["UV_NO_CONFIG"] == "1" and senv["UV_OFFLINE"] == "1")
    check("4b sync: the environment and any managed interpreter stay inside the versioned tree",
          senv["UV_PROJECT_ENVIRONMENT"].startswith(str(REPO))
          and senv["UV_PYTHON_INSTALL_DIR"].startswith(str(REPO / "tools")))


# --- 4a: the two implementations agree, and the O_NONBLOCK inversion --------------------------------
# `.local/bin/plainkeep-core` is a BUILD ARTIFACT and it is gitignored, so a checkout can carry one
# that predates the source beside it. PRESENCE is therefore the wrong question — this suite asked it
# and paid for it: against a core built before Task 4, every parity cell compared `core=''` (the
# binary answered "unknown verb") to a real Python value and went red, reporting a defect in the code
# under test when the true fact was "your build is stale".
#
# CURRENCY is the right question, and the binary already publishes the answer: `--core-api intercepts`
# exists so a harness never has to mirror what the binary handles. If `--core-provision` is not in
# that list the cells SKIP, loudly, naming the one command that fixes it — a skip that says why beats
# a red that misattributes.
def core_speaks_provision(core: Path) -> bool:
    r = run(str(core), "--core-api", "intercepts")
    if r.returncode != 0:
        return False
    try:
        return "--core-provision" in json.loads(r.stdout)["flags"]["always"]
    except (ValueError, KeyError, TypeError):
        return False


STALE_CORE = ("core-parity cells (the compiled plainkeep-core predates this task — it does not "
              "publish --core-provision. Rebuild it: cd cli && bun run build)")


def case_core_parity(tmp: Path) -> None:
    if not CORE.is_file():
        skipped.append("core-parity cells (no compiled plainkeep-core — build it: cd cli && bun run build)")
        return
    if not core_speaks_provision(CORE):
        skipped.append(STALE_CORE)
        return
    engine = install_engine(tmp / "parity")
    core = engine / ".local" / "bin" / "plainkeep-core"
    if not core.is_file():
        skipped.append("core-parity cells (the installed engine carries no core binary)")
        return
    # Asked of the INSTALLED copy too, not just the checkout's: `_copy_owned` copies whatever was
    # there, and the two can differ if a build lands mid-run.
    if not core_speaks_provision(core):
        skipped.append(STALE_CORE)
        return

    def both(*args: str) -> tuple[subprocess.CompletedProcess, subprocess.CompletedProcess]:
        return (run(str(core), "--core-provision", *args),
                prov(engine, "--print", *args))

    for what in ("target", "uv", "env", "offline"):
        c, p = both(what)
        check(f"4a parity: the core and the Python module agree on `{what}`, byte for byte",
              c.stdout.strip() == p.stdout.strip() and c.returncode == p.returncode == 0,
              f"core={c.stdout.strip()[:80]!r} py={p.stdout.strip()[:80]!r}")
    c = run(str(core), "--core-provision", "pin")
    p = prov(engine, "--print", "pin")
    check("4a parity: the same pin, the same target, the same digest, the same destination",
          json.loads(c.stdout or "{}") == json.loads(p.stdout or "{}"),
          (c.stdout or c.stderr)[:200])

    c = run(str(core), "--core-provision", "ensure-uv", "--offline")
    check("4a parity: the core refuses offline with the same three facts the module gives",
          c.returncode != 0 and "shasum -a 256 -c -" in c.stderr, c.stderr[:200])

    # THE CHECKSUM GATE ON THE CORE PATH. On a machine with no system python3 this IS the
    # provisioning path, so 4b's "a tampered lock fails its checksum rather than installing" has to
    # hold here too — a version of the gate that only the Python side enforced would hold exactly on
    # the machines that do not need it.
    c = run(str(core), "--core-provision", "sync", "--offline")
    check("4b: an INTACT tree gets past the core's checksum gate (to the offline uv step)",
          "recorded checksums" not in c.stderr, c.stderr[:200])
    unseal_write(engine / "uv.lock", (engine / "uv.lock").read_text(encoding="utf-8") + "\n# tampered\n")
    c = run(str(core), "--core-provision", "sync", "--offline")
    check("4b GATE: the CORE refuses a tampered lock, on the checksum, before uv is downloaded",
          c.returncode == EXIT_DENY and "uv.lock does not match its recorded checksum" in c.stderr
          and "cannot download uv" not in c.stderr, f"rc={c.returncode} {c.stderr[:250]}")
    check("4b: and the Python module reaches the same verdict about the same tree",
          enginetree.digest_problems(engine, only=("pyproject.toml", "uv.lock")) != [])

    # The pinned engine interpreter — ADR-013's carried inversion, and what dispatch.ts now spawns.
    c = run(str(core), "--core-provision", "python")
    check("4a: an UNPROVISIONED engine reports no interpreter (exit 4) rather than guessing one",
          c.returncode == EXIT_NOT_FOUND, f"rc={c.returncode} {c.stdout[:120]}")
    check("4a: and the Python module agrees it is not there",
          provision.engine_python(engine) is None, str(provision.engine_python(engine)))

    venvbin = engine / enginetree.PROVISION_DIR / provision.VENV_DIRNAME / "bin"
    venvbin.mkdir(parents=True)
    (venvbin / "python3").write_text("#!/bin/sh\nexec true\n", encoding="utf-8")
    (venvbin / "python3").chmod(0o755)
    c = run(str(core), "--core-provision", "python")
    # Compared through realpath: the core canonicalizes its engine root from `execPath` (ADR-017 D2),
    # and a macOS temp directory is reached as both /var/... and /private/var/....
    same = (c.returncode == 0
            and os.path.realpath(c.stdout.strip())
            == os.path.realpath(str(provision.engine_python(engine) or "")))
    check("4a: a PROVISIONED engine reports its own interpreter, and both implementations name the "
          "same path (this is what the O_NONBLOCK helper now spawns instead of a bare `python3`)",
          same, f"core={c.stdout.strip()[:120]!r} py={provision.engine_python(engine)}")
    check("4a: the interpreter it names is INSIDE the engine, not a host python",
          Path(os.path.realpath(c.stdout.strip())).is_relative_to(os.path.realpath(engine)),
          c.stdout.strip()[:120])


# --- 1c: doctor's provisioning rows say something an operator can act on --------------------------
def case_doctor_names_the_provisioning_command(tmp: Path) -> None:
    """DOCTOR MUST NAME A COMMAND THAT ACTUALLY PROVISIONS.

    The row used to read "`plainkeep setup` fetches it". It does not: no `plainkeep` verb reaches
    `provision.ensure_uv` or `provision.sync()` — the only entry points are the module CLI and
    `plainkeep-core --core-provision`. An operator following the row ran `plainkeep setup`, got a
    vault `.venv` pip install, and watched the row not change.

    All three of the 1c rows are pinned here, because the r1 review's second point about them is that
    deleting the whole block left every suite green: nothing would have caught the text going stale
    either. Driving the verb and grepping its output is the cheapest thing that would have."""
    engine = install_engine(tmp / "doctorrows")
    r = run(PY, str(engine / "bin" / "doctor" / "run.py"))
    out = r.stdout + r.stderr
    rows = [ln for ln in out.splitlines() if "engine:" in ln]
    check("1c doctor: the unprovisioned-uv row NAMES the command that provisions",
          any("--core-provision sync" in ln for ln in rows), " || ".join(rows)[:300])
    check("1c doctor: and it no longer claims `plainkeep setup` fetches the pinned uv — it does not, "
          "and an operator who ran it got a vault .venv pip install instead",
          not any("`plainkeep setup` fetches it" in ln for ln in rows), " || ".join(rows)[:300])
    # The command the row names has to BE a command. A row naming a spec the core rejects would be
    # the same defect with a different string in it.
    if CORE.is_file() and core_speaks_provision(CORE):
        c = run(str(CORE), "--core-api", "intercepts")
        check("1c doctor: `--core-provision` is a spec the core really accepts, so the row's advice "
              "resolves to a real command",
              "--core-provision" in json.loads(c.stdout)["flags"]["always"], c.stdout[:200])
    else:
        skipped.append("the doctor-row cell that confirms `--core-provision` is a live core spec ("
                       + ("no compiled plainkeep-core — build it: cd cli && bun run build"
                          if not CORE.is_file() else STALE_CORE) + ")")
    check("1c doctor: the unprovisioned-interpreter row is present and states the stdlib floor "
          "(an unprovisioned engine is a NORMAL state, not a broken one)",
          any("no provisioned interpreter yet" in ln for ln in rows), " || ".join(rows)[:300])
    # NOT doctor's exit code: a fixture vault legitimately fails other rows (no index), and asserting
    # rc==0 here would make this cell about the fixture rather than about the 1c block. The claim is
    # that an unprovisioned engine is a NORMAL state — so every one of these rows rides in the `ok`
    # bucket, none is a WARN and none a FAIL.
    check("1c doctor: every provisioning row is an `ok` row — an unprovisioned engine is a normal "
          "state (the stdlib floor is the contract, ADR-009), not a broken one",
          all("ok" in re.sub(r"\033\[[0-9;]*m", "", ln).split("engine:")[0] for ln in rows),
          " || ".join(re.sub(r"\033\[[0-9;]*m", "", ln) for ln in rows)[:300])
    # The system-uv row is conditional on the machine having one, so it is asserted only where it
    # applies rather than skipped wholesale — the claim it carries is the one D3 promises operators.
    if provision.system_uv():
        check("1c doctor: a system uv present on this machine is reported as IGNORED",
              any("is IGNORED" in ln for ln in rows), " || ".join(rows)[:300])
    else:
        skipped.append("the doctor system-uv row (no system uv on this machine to be ignored)")


def main() -> int:
    global FIXTURE_HOME
    with tempfile.TemporaryDirectory(prefix="pk-provision-") as td:
        tmp = Path(td)
        FIXTURE_HOME = make_fixture_vault(tmp / "fixture-vault")
        case_matrix()
        case_pin()
        case_offline_refusal(tmp)
        case_system_uv_is_ignored(tmp)
        case_checksum_gate(tmp)
        case_pin_is_gated(tmp)
        case_injected_file_is_refused(tmp)
        case_seal_interaction(tmp)
        case_reseal_keeps_the_environment(tmp)
        case_delivered_lock(tmp)
        case_frozen_sync_offline(tmp)
        case_core_parity(tmp)
        case_product_consults_the_matrix(tmp)
        case_doctor_names_the_provisioning_command(tmp)
        # Installed trees are sealed 0555, which TemporaryDirectory cannot remove.
        for p in sorted(tmp.rglob("*"), key=lambda q: len(q.parts), reverse=True):
            try:
                if p.is_dir() and not p.is_symlink():
                    p.chmod(0o755)
            except OSError:
                pass

    print(f"{BOLD}provisioning: uv bootstrap + delivered lock + frozen matrix "
          f"(ADR-020 / Phase 2 Task 4) — {len(results)} checks{RESET}\n")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark} {name:<96}" + (f" {DIM}{str(detail).strip()[:110]}{RESET}"
                                        if (detail and not ok) else ""))
    failed = len(results) - passed
    for s in skipped:
        print(f"SUITE-NOTE: SKIPPED — {s}")
    print("SUITE-NOTE: every cell here is OFFLINE. The real download and the real `uv sync` against "
          "PyPI are NOT exercised by this suite; they were measured by hand for Task 4 and recorded "
          "in its report. What is proved here is the mechanism around them — the pin, the checksum "
          "refusal, the seal interaction, the delivered lock's digests, and a real `uv sync --frozen` "
          "against wheels this file builds.")
    print(f"\n{BOLD}Result:{RESET} {GREEN}{passed} passed{RESET}, "
          f"{(RED if failed else DIM)}{failed} failed{RESET}, "
          f"{len(results)} checks, {len(skipped)} skipped group(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
