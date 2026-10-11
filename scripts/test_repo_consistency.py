#!/usr/bin/env python3
"""Static consistency checks for the runner recipes and the catalogue.

stdlib only, not pytest, same as the other scripts/test_*.py:

    python3 scripts/test_repo_consistency.py

No network, no container runtime. Checks:
  * images.json is schema hal0.runner-images.v2 (docs/SCHEMA.md), with no
    unknown fields, and every recipe has exactly one matching entry;
  * every runners/<key>/manifest.toml passes scripts/runner.py's validation
    and carries [verification] and [lifecycle] (with a release-aware track);
  * every Containerfile takes its base images from manifest build args pinned
    by digest, declares no pin defaults of its own, and clones no branch;
  * Python dependency files pin exact versions;
  * runners/standard and runners/cpu take llama.cpp from runners/llama.cpp.toml,
    and the external cuda entry does not run ahead of that release;
  * no retired names or in-image paths in the recipes, scripts or catalogue;
  * retention-allowlist.json refs parse; no private LAN addresses anywhere.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import runner  # noqa: E402
from retention import parse_allowlist_ref  # noqa: E402

#: Mirror of hal0 src/hal0/runners/__init__.py RuntimeFamily.
FAMILIES = {"llama-server", "flm", "kokoro", "qwen3tts", "moonshine", "comfyui"}
BACKENDS = {"rocm", "vulkan", "cpu", "npu", "cuda"}
TIERS = {"default", "optional"}
ENTRY_FIELDS = ["id", "title", "summary", "image", "tag", "digest", "candidate", "gate_report",
                "lanes_gated", "backends", "family", "tier", "recipe", "maintainer_notes"]
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
PINNED_IMAGE = re.compile(r"^[A-Za-z0-9._\-/:]+@sha256:[0-9a-f]{64}$")
TRACK_KINDS = {"github-release", "pypi"}
#: Build args every recipe gets from scripts/runner.py itself.
TOOL_ARGS = {"CMAKE_FLAGS", "JOBS"}
#: Names of the retired runner system. None of them may come back in the
#: recipes, the scripts or the catalogue (retention-allowlist.json and the
#: docs' history notes are the only places they are still named).
RETIRED = ["hal0-combined", "hal0-toolbox-", "rocmfpx", "strix-vulkan", "promptforge",
           "hal0-strix-base", "llama-vulkan", "/opt/rocmfpx", "/opt/llama-vulkan"]

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def raw_manifests() -> dict[str, dict]:
    return {k: tomllib.loads((ROOT / "runners" / k / "manifest.toml").read_text()) for k in runner.keys()}


def manifests() -> dict[str, dict]:
    """Manifests with the shared llama.cpp pin filled in (scripts/runner.py resolve)."""
    return {k: runner.resolve(k, doc) for k, doc in raw_manifests().items()}


def catalogue() -> dict:
    return json.loads((ROOT / "images.json").read_text())


def test_catalogue_shape() -> None:
    doc = catalogue()
    check(list(doc) == ["schema", "runners"], f"images.json: top-level keys must be schema, runners; got {list(doc)}")
    check(doc.get("schema") == "hal0.runner-images.v2", "images.json: schema must be hal0.runner-images.v2")
    entries = doc.get("runners")
    check(isinstance(entries, list) and entries, "images.json: runners must be a non-empty array")
    seen: set[str] = set()
    for e in entries or []:
        i = e.get("id", "?")
        check(list(e) == ENTRY_FIELDS, f"images.json[{i}]: fields must be exactly {ENTRY_FIELDS}, got {list(e)}")
        check(isinstance(i, str) and runner.KEY.match(i) is not None, f"images.json[{i}]: id must be a runner key")
        check(i not in seen, f"images.json: duplicate id {i}")
        seen.add(i)
        for f in ("title", "summary", "image", "maintainer_notes"):
            check(isinstance(e.get(f), str) and e[f].strip() != "", f"images.json[{i}]: {f} must be a non-empty string")
        check("\n" not in e.get("summary", ""), f"images.json[{i}]: summary must be one line")
        check(e.get("family") in FAMILIES, f"images.json[{i}]: family {e.get('family')!r} not a hal0 RuntimeFamily")
        check(e.get("tier") in TIERS, f"images.json[{i}]: tier must be one of {sorted(TIERS)}")
        b = e.get("backends")
        check(isinstance(b, list) and b and set(b) <= BACKENDS, f"images.json[{i}]: backends {b!r} invalid")
        tag, digest = e.get("tag"), e.get("digest")
        check(tag is None or (isinstance(tag, str) and tag and ":" not in tag),
              f"images.json[{i}]: tag must be null or a bare tag")
        check(digest is None or (isinstance(digest, str) and DIGEST.match(digest) is not None),
              f"images.json[{i}]: digest must be null or sha256:<64 hex>")
        check(digest is None or tag is not None, f"images.json[{i}]: a digest needs its tag")
        cand = e.get("candidate")
        if cand is not None:
            check(isinstance(cand, dict) and list(cand) == ["tag", "digest"]
                  and isinstance(cand["tag"], str) and ":" not in cand["tag"]
                  and isinstance(cand["digest"], str) and DIGEST.match(cand["digest"]) is not None,
                  f"images.json[{i}]: candidate must be null or {{tag, digest}} with a bare tag and sha256 digest")
        report = e.get("gate_report")
        check(report is None or (isinstance(report, str) and report.startswith("https://")),
              f"images.json[{i}]: gate_report must be null or an https URL")
        lanes = e.get("lanes_gated")
        check(isinstance(lanes, list) and set(lanes) <= set(b or []) and len(lanes) == len(set(lanes)),
              f"images.json[{i}]: lanes_gated must be a list of distinct entries from backends")
        if lanes:
            check(digest is not None and report is not None,
                  f"images.json[{i}]: lanes_gated needs the gated digest and its gate_report")
        rec = e.get("recipe")
        if rec is None:
            check(not e["image"].startswith(runner.PACKAGE_PREFIX),
                  f"images.json[{i}]: an external entry (recipe null) cannot use a hal0-runner package")
        else:
            check(rec == f"runners/{i}" and (ROOT / rec / "manifest.toml").is_file(),
                  f"images.json[{i}]: recipe must be runners/{i} and exist")
            check(e["image"] == runner.PACKAGE_PREFIX + i, f"images.json[{i}]: image must be {runner.PACKAGE_PREFIX}{i}")
            for t in (tag, (cand or {}).get("tag")):
                if t is not None:
                    check(re.match(r"^[A-Za-z0-9._]+-r[0-9a-f]{7}$", t) is not None,
                          f"images.json[{i}]: a built tag is <version>-r<sha7>, got {t!r}")


def test_catalogue_matches_recipes() -> None:
    entries = {e["id"]: e for e in catalogue()["runners"]}
    for key, doc in manifests().items():
        e = entries.get(key)
        check(e is not None, f"runners/{key}: no images.json entry")
        if e is None:
            continue
        r = doc["runner"]
        for field, want in (("title", r["title"]), ("summary", r["summary"]), ("backends", r["backends"]),
                            ("family", r["family"]), ("tier", r["tier"]), ("image", doc["image"]["package"])):
            check(e[field] == want, f"images.json[{key}].{field} != runners/{key}/manifest.toml ({e[field]!r} vs {want!r})")
        if e["tag"] is not None:
            check(e["tag"].startswith(doc["image"]["version"] + "-r"),
                  f"images.json[{key}].tag {e['tag']!r} is not a build of version {doc['image']['version']}")


def test_manifests() -> None:
    for key, doc in manifests().items():
        for p in runner.validate(key, doc):
            failures.append(f"runners/{key}: {p}")
        check(doc["runner"]["family"] in FAMILIES, f"runners/{key}: family not a hal0 RuntimeFamily")
        check(doc["runner"]["tier"] in TIERS, f"runners/{key}: tier invalid")
        check(set(doc["runner"]["backends"]) <= BACKENDS, f"runners/{key}: backends invalid")
        probes = doc.get("verification", {}).get("probes")
        check(isinstance(probes, list) and len(probes) > 0, f"runners/{key}: [verification].probes must list the gate")
        life = doc.get("lifecycle", {})
        for f in ("purpose", "retire_when"):
            check(isinstance(life.get(f), str) and life[f].strip() != "", f"runners/{key}: [lifecycle].{f} missing")
        check(isinstance(life.get("needed_for"), list) and life["needed_for"],
              f"runners/{key}: [lifecycle].needed_for must name at least one model or feature")
        track = life.get("track", {})
        kind = track.get("kind")
        check(kind in TRACK_KINDS, f"runners/{key}: [lifecycle.track].kind must be one of {sorted(TRACK_KINDS)}")
        src_kind = doc["source"].get("kind", "git")
        if kind == "github-release":
            check(src_kind == "git", f"runners/{key}: a github-release track needs a git source")
            check(bool(track.get("pattern")), f"runners/{key}: github-release track needs a tag pattern")
            rel = doc["source"].get("release")
            if rel:
                check(re.match(track.get("pattern", "$^"), rel) is not None,
                      f"runners/{key}: [source].release {rel!r} does not match the track pattern")
        if kind == "pypi":
            check(src_kind == "pypi", f"runners/{key}: a pypi track needs a pypi source")
            check(doc["image"]["version"] == doc["source"].get("version"),
                  f"runners/{key}: [image].version must equal the pinned PyPI version")
        flags = doc.get("build", {}).get("cmake_flags")
        if flags is not None:
            check("-DGGML_NATIVE=OFF" in flags and "-DGGML_NATIVE=ON" not in flags,
                  f"runners/{key}: set -DGGML_NATIVE=OFF; ggml otherwise builds for the build machine's CPU (hal0 #2126)")
            check("-DLLAMA_BUILD_UI=OFF" in flags and "-DLLAMA_USE_PREBUILT_UI=OFF" in flags,
                  f"runners/{key}: turn the web UI off with upstream's current option names")


def containerfile_args(text: str) -> tuple[dict[str, str | None], list[str]]:
    """ARG name -> default (None when it has none), and every FROM operand."""
    args: dict[str, str | None] = {}
    for m in re.finditer(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(\S*))?\s*$", text, re.M):
        args.setdefault(m.group(1), m.group(2))
    froms = re.findall(r"^FROM\s+(\S+)", text, re.M)
    return args, froms


def test_containerfiles() -> None:
    for key, doc in manifests().items():
        rdir = ROOT / "runners" / key
        text = (rdir / doc["image"].get("containerfile", "Containerfile")).read_text()
        args, froms = containerfile_args(text)
        bargs = doc.get("build", {}).get("args", {})
        stages = set(re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)", text, re.M | re.I))
        for f in froms:
            m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", f)
            if m:
                name = m.group(1)
                check(name in bargs and PINNED_IMAGE.match(bargs[name]) is not None,
                      f"runners/{key}: FROM ${{{name}}} must come from [build.args] pinned as image@sha256:<digest>")
            else:
                check(f in stages, f"runners/{key}: FROM {f} must be a build arg pinned by digest, not a literal image")
        for name in bargs:
            check(name in args, f"runners/{key}: [build.args].{name} is not declared as an ARG in the Containerfile")
        for name, default in args.items():
            if name in bargs:
                check(default is None, f"runners/{key}: ARG {name} has a default; manifest.toml is the only place a pin lives")
            elif name not in TOOL_ARGS and name != "DEBIAN_FRONTEND":
                failures.append(f"runners/{key}: ARG {name} is not set by manifest.toml [build.args]")
        if doc.get("build", {}).get("cmake_flags"):
            check("CMAKE_FLAGS" in args, f"runners/{key}: cmake_flags set but the Containerfile has no ARG CMAKE_FLAGS")
        check(not re.search(r"git clone[^\n]*\s(-b|--branch)\s", text), f"runners/{key}: clones a branch; pin a commit")
        for name, val in bargs.items():
            check(val not in ("main", "master", "latest", "HEAD"), f"runners/{key}: [build.args].{name} is a moving ref")
        for src in re.findall(r"^COPY\s+(?!--from)(\S+)\s", text, re.M):
            if src.rstrip("/") == "src" and doc["source"].get("checkout"):
                continue
            check((rdir / src).exists(), f"runners/{key}: COPY source {src} missing from runners/{key}/")


def test_python_pins() -> None:
    pin = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(\[[A-Za-z0-9,_\-]+\])?==[A-Za-z0-9.+!_\-]+$")
    for f in sorted((ROOT / "runners").glob("*/requirements.txt")) + sorted((ROOT / "runners").glob("*/constraints.txt")):
        names = []
        for n, line in enumerate(f.read_text().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            check(pin.match(line) is not None, f"{f.relative_to(ROOT)}:{n}: {line!r} is not an exact name==version pin")
            names.append(line.split("==")[0].lower())
        check(len(names) == len(set(names)), f"{f.relative_to(ROOT)}: duplicate package")


def test_llama_release() -> None:
    """One llama.cpp release: runners/llama.cpp.toml, used by standard, cpu and cuda."""
    pin = runner.read_pin("llama.cpp.toml")
    check(re.fullmatch(r"b[0-9]+", pin.get("version", "")) is not None, "runners/llama.cpp.toml: version must be bNNNNN")
    check(runner.SHA40.match(pin.get("ref", "")) is not None, "runners/llama.cpp.toml: ref must be a full commit SHA")
    check(pin.get("repo") == "https://github.com/ggml-org/llama.cpp.git", "runners/llama.cpp.toml: repo must be ggml-org/llama.cpp")
    for key in ("standard", "cpu"):
        check(raw_manifests()[key]["source"].get("pin") == "llama.cpp.toml",
              f"runners/{key}: must take llama.cpp from runners/llama.cpp.toml ([source].pin)")
    cuda = next((e for e in catalogue()["runners"] if e["id"] == "cuda"), None)
    check(cuda is not None and cuda["recipe"] is None, "images.json: the external cuda entry is missing")
    if cuda is not None:
        check(cuda["image"] == "ghcr.io/ggml-org/llama.cpp", "images.json[cuda]: image must be ghcr.io/ggml-org/llama.cpp")
        for t in (cuda["tag"], (cuda["candidate"] or {}).get("tag")):
            if t is None:
                continue
            mt = re.fullmatch(r"server-cuda-b([0-9]+)", t)
            check(mt is not None, f"images.json[cuda]: {t!r} must be ggml-org's server-cuda-b<release>")
            if mt:
                check(int(mt.group(1)) <= int(pin["version"].lstrip("b")),
                      "images.json[cuda]: the CUDA image must not be newer than runners/llama.cpp.toml")


def test_standard_entrypoint() -> None:
    text = (ROOT / "runners" / "standard" / "entrypoint.sh").read_text()
    check('SERVER="${HAL0_RUNNER_SERVER:-/opt/hal0/bin/llama-server}"' in text,
          "runners/standard/entrypoint.sh: server must default to /opt/hal0/bin/llama-server")
    check('set -- "$@" --port "$port"' in text,
          "runners/standard/entrypoint.sh: must pass --port when the caller did not (upstream default is 9931)")
    cf = (ROOT / "runners" / "standard" / "Containerfile").read_text()
    check('ENTRYPOINT ["/opt/hal0/entrypoint.sh"]' in cf, "runners/standard: ENTRYPOINT must be /opt/hal0/entrypoint.sh")


def tracked_files() -> list[str]:
    return subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()


def test_no_retired_names() -> None:
    me = Path(__file__).relative_to(ROOT).as_posix()
    for f in tracked_files():
        if not (f.startswith(("runners/", "scripts/")) or f == "images.json") or f == me:
            continue
        p = ROOT / f
        if not p.is_file() or "/patches/" in f:
            continue
        text = p.read_text(errors="replace")
        for name in RETIRED:
            # retention.py's fixtures and docstrings legitimately name old packages.
            if name in text and not f.startswith("scripts/test_retention") and f != "scripts/retention.py":
                failures.append(f"{f}: mentions retired {name!r}")


def test_allowlist() -> None:
    doc = json.loads((ROOT / "retention-allowlist.json").read_text())
    for ref in doc.get("hal0_code_pins", []) + doc.get("evidence", {}).get("refs", []):
        parse_allowlist_ref(ref)  # raises SystemExit on a malformed ref


def test_no_lan_addresses() -> None:
    lan = re.compile(r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")
    for f in tracked_files():
        p = ROOT / f
        if p.is_symlink() or not p.is_file() or "/patches/" in f:
            continue
        try:
            text = p.read_text()
        except UnicodeDecodeError:
            continue
        for m in lan.finditer(text):
            failures.append(f"{f}: LAN address {m.group(0)}")


def main() -> int:
    for t in (test_catalogue_shape, test_catalogue_matches_recipes, test_manifests, test_containerfiles,
              test_python_pins, test_llama_release, test_standard_entrypoint, test_no_retired_names,
              test_allowlist, test_no_lan_addresses):
        before = len(failures)
        t()
        print(f"{'FAIL' if len(failures) > before else 'ok  '} {t.__name__}")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
