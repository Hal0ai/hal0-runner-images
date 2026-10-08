#!/usr/bin/env python3
"""Static consistency checks for the tracked recipes and registries.

stdlib only, NOT pytest, same as test_retention_fixtures.py:

    python3 scripts/test_repo_consistency.py

Checks that need no network and no container runtime:
  * images.json keeps the hal0.runner-images.v1 shape hal0's sync parses
    (src/hal0/registry/runner_image_sync.py) and RuntimeFamily vocabulary
    (src/hal0/runners/__init__.py).
  * build-recipes.json llama_ref == each Dockerfile's ARG LLAMA_REF default,
    and no recipe clones a moving branch.
  * runners/* manifests parse; the fork recipes FROM the consumed strix
    base digest; shared build.sh/entrypoint.sh symlinks are intact; the
    upstream recipe's tracked Containerfile matches its manifest.
  * retention-allowlist.json refs parse.
  * no private LAN addresses in tracked text files.
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

from retention import parse_allowlist_ref  # noqa: E402

# Mirror of hal0 src/hal0/runners/__init__.py RuntimeFamily.
RUNTIME_FAMILIES = {"llama-server", "flm", "kokoro", "qwen3tts", "moonshine", "comfyui"}
# CUDA is out of scope for hal0 runner images.
BACKENDS = {"rocm", "vulkan", "cpu", "npu"}
PUBLISH = {"ci", "external", "manual"}
OWNERSHIP = {"owned", "referenced"}
SHA40 = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def load_json(name: str) -> dict:
    return json.loads((ROOT / name).read_text())


def test_images_json() -> None:
    doc = load_json("images.json")
    check(doc.get("schema") == "hal0.runner-images.v1", "images.json: schema string changed")
    images = doc.get("images")
    check(isinstance(images, list), "images.json: images must be an array")
    seen: set[str] = set()
    for e in images or []:
        i = e.get("id")
        check(isinstance(i, str) and i, f"images.json: entry without id: {e}")
        check(i not in seen, f"images.json: duplicate id {i}")
        seen.add(i)
        check(isinstance(e.get("image"), str) and e["image"].startswith("ghcr.io/hal0ai/"),
              f"images.json[{i}]: image must be a ghcr.io/hal0ai ref")
        check(e.get("runtime_family") in RUNTIME_FAMILIES,
              f"images.json[{i}]: runtime_family {e.get('runtime_family')!r} not in hal0 RuntimeFamily")
        b = e.get("supported_backends")
        check(isinstance(b, list) and b and set(b) <= BACKENDS,
              f"images.json[{i}]: supported_backends {b!r} invalid")
        check(e.get("publish") in PUBLISH, f"images.json[{i}]: publish invalid")
        check(e.get("ownership") in OWNERSHIP, f"images.json[{i}]: ownership invalid")
        if "digest" in e:
            check(bool(DIGEST.match(e["digest"])), f"images.json[{i}]: malformed digest")
        if e.get("publish") == "ci":
            bd = e.get("build") or {}
            for k in ("context", "dockerfile"):
                check(bool(bd.get(k)) and (ROOT / bd[k]).exists(),
                      f"images.json[{i}]: publish=ci needs an existing build.{k}")
        for k in ("recipe",):
            ref = (e.get("build") or {}).get(k)
            if ref:
                check((ROOT / ref).exists(), f"images.json[{i}]: build.{k} {ref} missing")


def test_build_recipes() -> None:
    doc = load_json("build-recipes.json")
    check(doc.get("schema") == "hal0.build-recipes.v1", "build-recipes.json: schema string")
    img_ids = {e["id"] for e in load_json("images.json")["images"]}
    for r in doc.get("recipes", []):
        i = r["id"]
        check(i not in img_ids, f"build-recipes.json[{i}]: id collides with images.json")
        check(r["image"].startswith("ghcr.io/hal0ai/hal0-"),
              f"build-recipes.json[{i}]: must push to a hal0-* package (never the fork's)")
        check(bool(SHA40.match(r["llama_ref"])), f"build-recipes.json[{i}]: llama_ref must be a full SHA")
        df = (ROOT / r["dockerfile"]).read_text()
        m = re.search(r"^ARG LLAMA_REF=(\S+)$", df, re.M)
        check(bool(m) and m.group(1) == r["llama_ref"],
              f"{r['dockerfile']}: ARG LLAMA_REF default != build-recipes.json llama_ref")
        m = re.search(r"^ARG REPO=(\S+)$", df, re.M)
        check(bool(m) and m.group(1) == r["llama_repo"],
              f"{r['dockerfile']}: ARG REPO != build-recipes.json llama_repo")
        check("ARG BRANCH" not in df and not re.search(r"git clone[^\n]*\s-b\s", df),
              f"{r['dockerfile']}: clones a branch; pin a SHA")
        for f in re.findall(r"^COPY (?!--from)(\S+) ", df, re.M):
            check((ROOT / r["context"] / f).exists(), f"{r['dockerfile']}: COPY source {f} missing")


def test_runners() -> None:
    base = next(r for r in load_json("build-recipes.json")["recipes"] if r["id"] == "strix-base")
    base_img, base_digest = base["consumed_ref"].split("@")
    base_img = base_img.rsplit(":", 1)[0]
    for d in ("rocmfpx", "upstream", "strix", "promptforge"):
        rdir = ROOT / "runners" / d
        doc = tomllib.loads((rdir / "manifest.toml").read_text())
        check(bool(SHA40.match(doc["source"]["ref"])), f"runners/{d}: source.ref not a full SHA")
        check(bool(DIGEST.match(doc["base"]["digest"])), f"runners/{d}: base.digest malformed")
        check("CANONICAL HOME" in (rdir / "manifest.toml").read_text()[:400],
              f"runners/{d}: missing canonical-home note")
        for p in doc.get("patches", []):
            check((rdir / "patches" / p["file"]).is_file(), f"runners/{d}: patch {p['file']} missing")
        lineage = doc["base"].get("lineage")
        if d == "promptforge" or lineage is not None:
            continue
        check(doc["base"]["image"].startswith(base_img) and doc["base"]["digest"] == base_digest,
              f"runners/{d}: [base] must stay on the consumed strix base digest")
    # Fork recipes share rocmfpx's generated-Containerfile build.sh; upstream
    # has a tracked Containerfile and its own build.sh (same --check contract).
    for d, files in (("strix", ("build.sh", "entrypoint.sh")), ("upstream", ("entrypoint.sh",))):
        for f in files:
            p = ROOT / "runners" / d / f
            check(p.is_symlink() and p.resolve() == (ROOT / "runners" / "rocmfpx" / f).resolve(),
                  f"runners/{d}/{f}: must symlink to ../rocmfpx/{f}")


def test_upstream_containerfile() -> None:
    """runners/upstream: the tracked Containerfile's ARG defaults match the
    manifest (build.sh passes the manifest values; the defaults are what a
    bare `docker build` gets, and must not drift)."""
    rdir = ROOT / "runners" / "upstream"
    doc = tomllib.loads((rdir / "manifest.toml").read_text())
    base = doc["base"]
    check(base.get("lineage") == "fedora44-rocm10", "runners/upstream: [base].lineage changed; update this test")
    check(bool(DIGEST.match(base["builder_digest"])), "runners/upstream: base.builder_digest malformed")
    cf = (rdir / "Containerfile").read_text()
    args = dict(re.findall(r"^ARG ([A-Z_]+)=(\S+)$", cf, re.M))
    check(args.get("FEDORA_BUILDER") == f"{base['builder_image']}@{base['builder_digest']}",
          "runners/upstream/Containerfile: ARG FEDORA_BUILDER != manifest builder image@digest")
    check(args.get("FEDORA_RUNTIME") == f"{base['image']}@{base['digest']}",
          "runners/upstream/Containerfile: ARG FEDORA_RUNTIME != manifest image@digest")
    check(args.get("ROCM_REPO") == base["rocm_repo"], "runners/upstream/Containerfile: ARG ROCM_REPO != manifest")
    check(args.get("ROCM_SERIES") == base["rocm_series"], "runners/upstream/Containerfile: ARG ROCM_SERIES != manifest")
    check(args.get("ROCM_NEVR") == base["rocm_nevr"], "runners/upstream/Containerfile: ARG ROCM_NEVR != manifest")
    check(base["rocm_version"].startswith(base["rocm_series"]) and base["rocm_nevr"].startswith(base["rocm_version"]),
          "runners/upstream: rocm_series / rocm_version / rocm_nevr disagree")
    check("@sha256:" in args.get("FEDORA_BUILDER", "") and "@sha256:" in args.get("FEDORA_RUNTIME", ""),
          "runners/upstream/Containerfile: FROM images must be digest-pinned")
    check("ENTRYPOINT [\"/opt/rocmfpx/hal0-runner-entrypoint.sh\"]" in cf,
          "runners/upstream/Containerfile: entrypoint must stay the shared hal0 runner entrypoint")
    check("LLAMA_BUILD_WEBUI=OFF" in " ".join(doc["build"]["cmake_flags"]),
          "runners/upstream: the web UI is not used; keep -DLLAMA_BUILD_WEBUI=OFF")


#: images.json entries whose `tag` a CI build may move. Every other publish:ci
#: image gets only an immutable `<tag>-r<commit>` tag (build-matrix.yml), so a
#: build can never change what a hal0 install pulls by tag.
MOVING_TAG_ALLOWED = {"comfyui"}


def test_moving_tags() -> None:
    for e in load_json("images.json")["images"]:
        mt = e.get("moving_tag", False)
        check(isinstance(mt, bool), f"images.json {e['id']}: moving_tag must be a bool")
        if mt:
            check(e["id"] in MOVING_TAG_ALLOWED,
                  f"images.json {e['id']}: moving_tag would let CI move a tag hal0 pulls; "
                  "move it through a bump (docs/BUMPING.md) instead")

def test_lifecycle() -> None:
    """Every runner says why it exists, when it retires, and what to watch."""
    for d in ("rocmfpx", "upstream", "strix", "promptforge"):
        doc = tomllib.loads((ROOT / "runners" / d / "manifest.toml").read_text())
        life = doc.get("lifecycle", {})
        for key in ("purpose", "retire_when", "track_ref"):
            check(isinstance(life.get(key), str) and life[key].strip() != "",
                  f"runners/{d}: [lifecycle].{key} missing or empty")
        check(isinstance(life.get("needed_for"), list) and len(life["needed_for"]) > 0,
              f"runners/{d}: [lifecycle].needed_for must list at least one model or feature")
        check(str(life.get("track_ref", "")).startswith("refs/heads/"),
              f"runners/{d}: [lifecycle].track_ref must be a refs/heads/ branch")
        img = doc.get("ci", {}).get("image", "")
        check(img == f"ghcr.io/hal0ai/hal0-runner-{d}",
              f"runners/{d}: [ci].image must be ghcr.io/hal0ai/hal0-runner-{d}, got {img!r}")
        check(img.rsplit("/", 1)[-1] != doc["image"]["tag"].split(":")[0].rsplit("/", 1)[-1],
              f"runners/{d}: [ci].image must not be the consumed package")
        flags = doc["build"]["cmake_flags"]
        check("-DGGML_NATIVE=OFF" in flags and "-DGGML_NATIVE=ON" not in flags,
              f"runners/{d}: set -DGGML_NATIVE=OFF and name the ISA; ggml defaults to -march=native, "
              "which ties the CPU code to the build machine (#2126)")
    for r in load_json("build-recipes.json")["recipes"]:
        check(str(r.get("track_ref", "")).startswith("refs/heads/"),
              f"build-recipes.json {r['id']}: track_ref must be a refs/heads/ branch")


def test_allowlist() -> None:
    doc = load_json("retention-allowlist.json")
    for ref in doc.get("hal0_code_pins", []) + doc.get("evidence", {}).get("refs", []):
        parse_allowlist_ref(ref)  # raises SystemExit on a malformed ref


def test_no_lan_addresses() -> None:
    lan = re.compile(r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                           check=True).stdout.split()
    for f in files:
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
    for t in (test_images_json, test_build_recipes, test_runners, test_upstream_containerfile,
              test_lifecycle, test_moving_tags,
              test_allowlist,
              test_no_lan_addresses):
        before = len(failures)
        t()
        print(f"{'FAIL' if len(failures) > before else 'ok  '} {t.__name__}")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
