#!/usr/bin/env python3
"""Build tool for the runner recipes under runners/<key>/.

stdlib only. Every runner is built the same way, from its manifest.toml:

    python3 scripts/runner.py list [--only k1,k2]     # GitHub Actions matrix JSON
    python3 scripts/runner.py tag <key>               # <version>-r<recipe sha7>
    python3 scripts/runner.py stage <key>             # build context in $HAL0_RUNNER_BUILD_DIR
    python3 scripts/runner.py args <key>              # KEY=VALUE build args, one per line
    python3 scripts/runner.py labels <key> [--tag T]  # KEY=VALUE OCI labels, one per line
    python3 scripts/runner.py build <key> [--tag T]   # stage + docker/podman build (no push)
    python3 scripts/runner.py build <key> --check     # stage only: source + patches, no build

The tag is `<image.version>-r<sha7>`, where sha7 is the last commit that
touched runners/<key>/ or the shared pin file it uses (runners/llama.cpp.toml
for standard and cpu, whose [source].pin names it). An unchanged recipe therefore always maps to the
same tag, and a published tag is never pushed again. Computing it needs the
full git history (actions/checkout with fetch-depth: 0).

The build context is $HAL0_RUNNER_BUILD_DIR (default /tmp/hal0-runner-<key>).
It holds a copy of the recipe directory and, for recipes with
`source.checkout = true`, a full clone of the pinned source at ./src with the
patch series applied. CI's source-mirror step bundles that ./src.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNERS = ROOT / "runners"
RECIPE_REPO = "https://github.com/Hal0ai/hal0-runner-images"
PACKAGE_PREFIX = "ghcr.io/hal0ai/hal0-runner-"
MARKER = ".hal0-runner-build"

KEY = re.compile(r"^[a-z][a-z0-9]*$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._]*$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


class RecipeError(SystemExit):
    pass


def keys() -> list[str]:
    """Every runner key, in directory order."""
    return sorted(p.name for p in RUNNERS.iterdir() if (p / "manifest.toml").is_file())


def read_pin(name: str) -> dict:
    """The [llama_cpp] table of a shared pin file under runners/ (e.g. llama.cpp.toml)."""
    path = RUNNERS / name
    if "/" in name or not path.is_file():
        raise RecipeError(f"shared pin file runners/{name} does not exist")
    return tomllib.loads(path.read_text())["llama_cpp"]


def resolve(key: str, raw: dict) -> dict:
    """Fill [source] and [image].version from the shared pin file, if the recipe uses one.

    A recipe with `[source].pin = "llama.cpp.toml"` must not repeat repo, ref,
    release or version: the pin file is the only place they live.
    """
    doc = {k: (dict(v) if isinstance(v, dict) else v) for k, v in raw.items()}
    src = doc.get("source", {})
    pin = src.get("pin")
    if not pin:
        return doc
    dup = [k for k in ("repo", "ref", "release") if k in src] + (["version"] if "version" in doc.get("image", {}) else [])
    if dup:
        raise RecipeError(f"runners/{key}: uses the shared pin runners/{pin}, so it must not set {dup}")
    shared = read_pin(pin)
    src.update(repo=shared["repo"], ref=shared["ref"], release=shared["version"])
    doc["source"] = src
    doc.setdefault("image", {})["version"] = shared["version"]
    return doc


def load(key: str) -> dict:
    path = RUNNERS / key / "manifest.toml"
    if not path.is_file():
        raise RecipeError(f"unknown runner {key!r}: no {path.relative_to(ROOT)}")
    doc = resolve(key, tomllib.loads(path.read_text()))
    problems = validate(key, doc)
    if problems:
        raise RecipeError("runners/{}/manifest.toml:\n  {}".format(key, "\n  ".join(problems)))
    return doc


def validate(key: str, doc: dict) -> list[str]:
    """Structural checks shared by the build tool and the repo tests."""
    out: list[str] = []
    runner = doc.get("runner", {})
    image = doc.get("image", {})
    source = doc.get("source", {})
    if not KEY.match(key):
        out.append(f"key {key!r} must be lowercase letters and digits")
    if runner.get("key") != key:
        out.append(f"[runner].key must be {key!r} (the directory name)")
    for field in ("title", "summary", "family", "tier"):
        if not isinstance(runner.get(field), str) or not runner[field].strip():
            out.append(f"[runner].{field} missing")
    if not isinstance(runner.get("backends"), list) or not runner["backends"]:
        out.append("[runner].backends must be a non-empty list")
    if image.get("package") != PACKAGE_PREFIX + key:
        out.append(f"[image].package must be {PACKAGE_PREFIX}{key}")
    if not VERSION.match(str(image.get("version", ""))):
        out.append("[image].version must be a plain upstream version (letters, digits, '.', '_')")
    if not (RUNNERS / key / image.get("containerfile", "Containerfile")).is_file():
        out.append("[image].containerfile does not exist")
    if not isinstance(source.get("repo"), str) or not source["repo"].startswith("https://"):
        out.append("[source].repo must be an https URL")
    kind = source.get("kind", "git")
    if kind == "git":
        if not SHA40.match(str(source.get("ref", ""))):
            out.append("[source].ref must be a full 40-character commit SHA")
    elif kind == "pypi":
        pin = f"{source.get('package')}=={source.get('version')}"
        req = RUNNERS / key / "requirements.txt"
        if not req.is_file() or pin not in req.read_text().split():
            out.append(f"[source] is the PyPI package {pin}; requirements.txt must pin exactly that")
        if source.get("checkout"):
            out.append("[source].checkout only applies to git sources")
    else:
        out.append(f"[source].kind must be 'git' or 'pypi', not {kind!r}")
    for p in doc.get("patches", []):
        if not source.get("checkout"):
            out.append("[[patches]] needs source.checkout = true (patches apply on the host)")
        if not (RUNNERS / key / "patches" / p.get("file", "")).is_file():
            out.append(f"patch {p.get('file')!r} missing from patches/")
    for k, v in doc.get("build", {}).get("args", {}).items():
        if not isinstance(v, str):
            out.append(f"[build.args].{k} must be a string")
    return out


def recipe_paths(key: str) -> list[str]:
    """The paths a runner's build depends on: its directory plus any shared pin file."""
    paths = [f"runners/{key}"]
    pin = tomllib.loads((RUNNERS / key / "manifest.toml").read_text()).get("source", {}).get("pin")
    if pin:
        paths.append(f"runners/{pin}")
    return paths


def recipe_commit(key: str) -> tuple[str, bool]:
    """(last commit touching the recipe or its shared pin, uncommitted changes there)."""
    rel = recipe_paths(key)
    sha = subprocess.run(["git", "log", "-1", "--format=%H", "--", *rel], cwd=ROOT,
                         capture_output=True, text=True).stdout.strip()
    status = subprocess.run(["git", "status", "--porcelain", "--", *rel], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    if not sha:
        return "0" * 40, True
    return sha, bool(status)


def tag_for(key: str, doc: dict | None = None) -> str:
    doc = doc or load(key)
    sha, _ = recipe_commit(key)
    return f"{doc['image']['version']}-r{sha[:7]}"


def build_dir(key: str) -> Path:
    return Path(os.environ.get("HAL0_RUNNER_BUILD_DIR") or f"/tmp/hal0-runner-{key}")


def patch_files(key: str, doc: dict) -> list[Path]:
    return [RUNNERS / key / "patches" / p["file"] for p in doc.get("patches", [])]


def build_args(key: str, doc: dict) -> dict[str, str]:
    args = dict(doc.get("build", {}).get("args", {}))
    flags = doc.get("build", {}).get("cmake_flags")
    if flags:
        args["CMAKE_FLAGS"] = " ".join(flags)
    args["JOBS"] = os.environ.get("JOBS", "4")
    return args


def tag_part(ref: str) -> str:
    """The tag of an image reference ("repo:tag" -> "tag"); a bare tag is returned as is."""
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        return last.split(":", 1)[1]
    return last if "/" not in ref else "latest"


def labels(key: str, doc: dict, tag: str | None = None) -> dict[str, str]:
    sha, dirty = recipe_commit(key)
    tag = tag or tag_for(key, doc)
    patches = patch_files(key, doc)
    digest = hashlib.sha256(b"".join(p.read_bytes() for p in patches)).hexdigest() if patches else ""
    return {
        "org.opencontainers.image.title": key,
        "org.opencontainers.image.version": tag_part(tag),
        "org.opencontainers.image.source": doc["source"]["repo"].removesuffix(".git"),
        "org.opencontainers.image.revision": source_revision(doc),
        "org.opencontainers.image.description": doc["runner"]["summary"],
        "dev.hal0.runner.key": key,
        "dev.hal0.recipe.repo": RECIPE_REPO,
        "dev.hal0.recipe.revision": sha + ("-dirty" if dirty else ""),
        "dev.hal0.runner.patches": ",".join(p.name for p in patches),
        "dev.hal0.runner.patches.sha256": digest,
    }


def source_revision(doc: dict) -> str:
    """The upstream revision: a commit SHA, or `package==version` for PyPI."""
    src = doc["source"]
    if src.get("kind", "git") == "pypi":
        return f"{src['package']}=={src['version']}"
    return src["ref"]


def git(*argv: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def stage(key: str, doc: dict) -> Path:
    """Fill the build context: recipe files, plus the patched source checkout."""
    work = build_dir(key)
    if work.exists() and any(work.iterdir()) and not (work / MARKER).exists():
        raise RecipeError(f"{work} exists, is not empty and was not made by this tool; "
                          "refusing to clear it (set HAL0_RUNNER_BUILD_DIR)")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    (work / MARKER).write_text(key + "\n")
    for item in (RUNNERS / key).iterdir():
        if item.name in ("manifest.toml", "patches"):
            continue
        dest = work / item.name
        shutil.copytree(item, dest) if item.is_dir() else shutil.copy2(item, dest)
    src = doc["source"]
    if src.get("checkout"):
        target = work / "src"
        # Full clone, not --depth 1: CI's source mirror is a `git bundle` of
        # this checkout, and git refuses to bundle a shallow repository.
        print(f"==> clone   {src['repo']}", file=sys.stderr)
        git("clone", "-q", "--no-checkout", src["repo"], str(target))
        git("checkout", "-q", src["ref"], cwd=target)
        head = git("rev-parse", "HEAD", cwd=target)
        if head != src["ref"]:
            raise RecipeError(f"checkout landed on {head}, manifest pins {src['ref']}")
        print(f"==> source  {head}", file=sys.stderr)
        for p in patch_files(key, doc):
            git("apply", "--check", str(p), cwd=target)
            git("apply", str(p), cwd=target)
            print(f"==> patch   {p.name}", file=sys.stderr)
    return work


def runtime() -> str:
    rt = os.environ.get("HAL0_CONTAINER_RUNTIME") or shutil.which("docker") or shutil.which("podman")
    if not rt:
        raise RecipeError("no docker or podman on PATH")
    return rt


def cmd_build(key: str, tag: str | None, check: bool) -> int:
    doc = load(key)
    work = stage(key, doc)
    if check:
        print(f"==> --check: runners/{key} staged in {work}; nothing built", file=sys.stderr)
        return 0
    tag = tag or f"{doc['image']['package']}:{tag_for(key, doc)}"
    argv = [runtime(), "build", "-f", str(work / doc["image"].get("containerfile", "Containerfile")),
            "-t", tag]
    for k, v in build_args(key, doc).items():
        argv += ["--build-arg", f"{k}={v}"]
    for k, v in labels(key, doc, tag).items():
        argv += ["--label", f"{k}={v}"]
    argv.append(str(work))
    print(f"==> build   {tag}", file=sys.stderr)
    subprocess.run(argv, check=True)
    print(f"==> built   {tag}", file=sys.stderr)
    return 0


def cmd_list(only: set[str]) -> int:
    known = keys()
    unknown = only - set(known)
    if unknown:
        raise RecipeError(f"unknown runner key(s): {sorted(unknown)}; known: {known}")
    legs = []
    for key in known:
        if only and key not in only:
            continue
        doc = load(key)
        b = doc.get("build", {})
        legs.append({
            "key": key,
            "image": doc["image"]["package"],
            "tag": tag_for(key, doc),
            "source_repo": doc["source"]["repo"],
            "source_kind": doc["source"].get("kind", "git"),
            "source_ref": source_revision(doc),
            "checkout": bool(doc["source"].get("checkout")),
            "heavy": bool(b.get("heavy")),
            "timeout_minutes": int(b.get("timeout_minutes", 90)),
        })
    print(json.dumps({"include": legs}))
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]

    def opt(name: str) -> str | None:
        if name in rest:
            i = rest.index(name)
            val = rest[i + 1]
            del rest[i:i + 2]
            return val
        return None

    if cmd == "list":
        only = opt("--only") or ""
        return cmd_list({s.strip() for s in only.split(",") if s.strip()})
    tag = opt("--tag")
    check = "--check" in rest
    rest = [a for a in rest if a != "--check"]
    if len(rest) != 1:
        raise RecipeError(f"usage: runner.py {cmd} <key>")
    key = rest[0]
    if cmd == "tag":
        print(tag_for(key))
    elif cmd == "stage":
        print(stage(key, load(key)))
    elif cmd == "args":
        for k, v in build_args(key, load(key)).items():
            print(f"{k}={v}")
    elif cmd == "labels":
        for k, v in labels(key, load(key), tag).items():
            print(f"{k}={v}")
    elif cmd == "build":
        return cmd_build(key, tag, check)
    else:
        raise RecipeError(f"unknown command {cmd!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
