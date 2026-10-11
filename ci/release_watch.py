#!/usr/bin/env python3
"""Prepare ONE llama.cpp bump when ggml-org has a newer release.

    python3 ci/release_watch.py --json-file result.json --body-file pr.md            # report only
    python3 ci/release_watch.py --apply --json-file result.json --body-file pr.md    # also rewrite files

`standard`, `cpu` and the external `cuda` entry share one llama.cpp release,
runners/llama.cpp.toml (`[llama_cpp]` version, ref, ref_description). --apply
rewrites that file when ggml-org's newest `b<N>` release is newer, and nothing
else: the runner manifests only point at it.

The `cuda` row of images.json follows the pin loosely. ggml-org does not publish
server-cuda for every release, so the row takes the newest `server-cuda-b<M>` tag
in GHCR with M at or below the pinned release, as `tag` and `digest` (the digest
that tag resolves to now). There is no hardware gate for it (the CUDA lane is
hidden in hal0), so it is set here, ungated, as the recipes schema describes.
When only cuda has a newer tag, the PR moves only that row. hal0's own pin for
cuda still moves through promote.yml.

The other runners are report-only; that is scripts/source_watch.py, which the
workflow runs next to this. This builds nothing, pushes nothing and calls only
public APIs.

ggml-org tags every `b<N>` release as a *prerelease*, so GET /releases/latest
returns an old unrelated release (v0.6.0 on 2026-10-11). We list releases and
take the highest `b<N>` instead.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

# ---------------------------------------------------------------------------
# THE ONE MAPPING to adjust if the recipes side moves the pin.
PIN_FILE = "runners/llama.cpp.toml"
PIN_TABLE = "llama_cpp"
PIN_VERSION_KEY = "version"              # release tag, e.g. "b11552"
PIN_REF_KEY = "ref"                      # full commit sha of that tag
PIN_DESC_KEY = "ref_description"         # "release <tag>: <commit subject>"
UPSTREAM = "ggml-org/llama.cpp"
TAG_RE = r"^b\d+$"
CATALOGUE = "images.json"
CUDA_ID = "cuda"                         # the images.json row that follows the pin
CUDA_IMAGE = "ghcr.io/ggml-org/llama.cpp"
CUDA_TAG_RE = re.compile(r"^server-cuda-b(\d+)$")
# ---------------------------------------------------------------------------

SHA40 = re.compile(r"^[0-9a-f]{40}$")
Api = Callable[[str], object]


@dataclass
class Bump:
    release: str                          # the pinned release after this change
    previous: str                         # the pinned release before it
    tag: str | None                       # new release when the pin moves, else None
    commit: str | None
    url: str | None
    compare_url: str | None
    cuda_previous: str | None             # the cuda row's tag before
    cuda_tag: str | None                  # the cuda row's new tag, else None
    cuda_digest: str | None = None
    branch: str = ""
    title: str = ""
    files: list[str] = field(default_factory=list)


def version_key(tag: str) -> tuple[int, ...] | None:
    nums = re.findall(r"\d+", tag)
    return tuple(int(n) for n in nums) if nums else None


def is_newer(candidate: str, current: str) -> bool:
    a, b = version_key(candidate), version_key(current)
    return a is not None and b is not None and a > b


def latest_release(api: Api) -> dict | None:
    rels = api(f"/repos/{UPSTREAM}/releases?per_page=100") or []
    pat = re.compile(TAG_RE)
    ok = [r for r in rels if not r.get("draft") and pat.match(r.get("tag_name", ""))]
    return max(ok, key=lambda r: version_key(r["tag_name"]) or (0,)) if ok else None


def resolve_commit(tag: str, api: Api) -> tuple[str, str]:
    """(sha, first line of the commit message) of a release tag."""
    data = api(f"/repos/{UPSTREAM}/commits/{tag}") or {}
    sha = data.get("sha", "")
    if not SHA40.match(sha):
        raise lib.CIError(f"could not resolve {UPSTREAM}@{tag} to a commit")
    subject = ((data.get("commit") or {}).get("message") or "").splitlines()[0:1]
    return sha, (subject[0] if subject else "")


# ---- the pin file ---------------------------------------------------------

def _kv_re(key: str) -> re.Pattern:
    return re.compile(r'^(\s*' + re.escape(key) + r'\s*=\s*)(["\'])(.*?)\2(.*)$')


def rewrite_pin(text: str, values: dict[str, str]) -> tuple[str, int]:
    """Set string keys inside [PIN_TABLE] only. Comments are never touched."""
    out, inside, changed = [], False, 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            inside = stripped.split("#")[0].strip() == f"[{PIN_TABLE}]"
        elif inside and not stripped.startswith("#"):
            for key, val in values.items():
                m = _kv_re(key).match(line.rstrip("\r\n"))
                if m:
                    nl = line[len(line.rstrip("\r\n")):]
                    safe = val.replace("\\", "\\\\").replace('"', '\\"')
                    line = f"{m.group(1)}{m.group(2)}{safe}{m.group(2)}{m.group(4)}{nl}"
                    changed += 1
                    break
        out.append(line)
    return "".join(out), changed


def read_pin(root: Path) -> dict:
    path = root / PIN_FILE
    if not path.is_file():
        raise lib.CIError(f"{PIN_FILE} does not exist")
    table = tomllib.loads(path.read_text()).get(PIN_TABLE) or {}
    for k in (PIN_VERSION_KEY, PIN_REF_KEY):
        if not isinstance(table.get(k), str) or not table[k]:
            raise lib.CIError(f"{PIN_FILE}: [{PIN_TABLE}].{k} missing or empty")
    return {"release": table[PIN_VERSION_KEY], "ref": table[PIN_REF_KEY]}


def apply_pin(root: Path, tag: str, commit: str, subject: str) -> str:
    path = root / PIN_FILE
    desc = f"release {tag}: {subject}" if subject else f"release {tag}"
    values = {PIN_VERSION_KEY: tag, PIN_REF_KEY: commit}
    if PIN_DESC_KEY in (tomllib.loads(path.read_text()).get(PIN_TABLE) or {}):
        values[PIN_DESC_KEY] = desc
    text, n = rewrite_pin(path.read_text(), values)
    parsed = tomllib.loads(text)[PIN_TABLE]
    if n != len(values) or parsed[PIN_VERSION_KEY] != tag or parsed[PIN_REF_KEY] != commit:
        raise lib.CIError(f"{PIN_FILE}: could not rewrite the pin ({n} of {len(values)} keys)")
    path.write_text(text)
    return PIN_FILE


# ---- the cuda row ---------------------------------------------------------

def newest_cuda_tag(tags: list[str], release: str) -> str | None:
    """The newest server-cuda-b<M> with M <= the pinned release number."""
    limit = (version_key(release) or (0,))[0]
    found = [(int(m.group(1)), t) for t in tags if (m := CUDA_TAG_RE.match(t)) and int(m.group(1)) <= limit]
    return max(found)[1] if found else None


def cuda_row(doc: dict) -> dict | None:
    rows = [e for e in doc.get("runners", []) if e.get("id") == CUDA_ID or e.get("key") == CUDA_ID]
    return rows[0] if len(rows) == 1 else None


def apply_cuda(root: Path, tag: str, digest: str) -> str:
    path = root / CATALOGUE
    raw = path.read_text()
    doc = json.loads(raw)
    row = cuda_row(doc)
    if row is None:
        raise lib.CIError(f"{CATALOGUE}: expected one {CUDA_ID!r} row")
    row["tag"] = tag
    row["digest"] = digest
    path.write_text(lib.dump_like(doc, raw))
    return CATALOGUE


def run(
    root: Path, api: Api, apply: bool,
    cuda_tags: Callable[[], list[str]] | None = None,
    cuda_digest: Callable[[str], str] | None = None,
) -> Bump | None:
    pin = read_pin(root)
    rel = latest_release(api)
    if not rel:
        raise lib.CIError(f"no {TAG_RE} release found for {UPSTREAM}")
    moves = is_newer(rel["tag_name"], pin["release"])
    release = rel["tag_name"] if moves else pin["release"]

    cuda_prev = cuda_new = None
    path = root / CATALOGUE
    row = cuda_row(json.loads(path.read_text())) if path.is_file() else None
    if row is not None:
        cuda_prev = row.get("tag")
        best = newest_cuda_tag((cuda_tags or (lambda: lib.ghcr_tags(CUDA_IMAGE)))(), release)
        if best and (cuda_prev is None or is_newer(best, cuda_prev)):
            cuda_new = best
    if not moves and not cuda_new:
        return None

    commit = subject = None
    if moves:
        commit, subject = resolve_commit(release, api)
    bump = Bump(
        release=release, previous=pin["release"], tag=release if moves else None, commit=commit,
        url=rel["html_url"] if moves else None,
        compare_url=f"https://github.com/{UPSTREAM}/compare/{pin['release']}...{release}" if moves else None,
        cuda_previous=cuda_prev, cuda_tag=cuda_new,
    )
    if cuda_new:
        bump.cuda_digest = (cuda_digest or (lambda t: lib.ghcr_digest(CUDA_IMAGE, t)))(cuda_new)
    cuda_suffix = f"cuda-{cuda_new.removeprefix('server-cuda-')}" if cuda_new else ""
    bump.branch = "-".join(x for x in (f"bot/bump-llama-cpp-{release}" if moves else "bot/bump-llama-cpp", cuda_suffix) if x)
    what = ([f"llama.cpp to {release}"] if moves else []) + ([f"cuda to {cuda_new}"] if cuda_new else [])
    bump.title = "chore(runners): bump " + " and ".join(what)
    if apply:
        if moves:
            bump.files.append(apply_pin(root, release, commit, subject or ""))  # type: ignore[arg-type]
        if cuda_new:
            bump.files.append(apply_cuda(root, cuda_new, bump.cuda_digest))  # type: ignore[arg-type]
    return bump


def pr_body(b: Bump) -> str:
    out: list[str] = []
    if b.tag:
        out += [f"Moves the shared llama.cpp pin (`runners/llama.cpp.toml`: `standard`, `cpu`) from `{b.previous}` to "
                f"[`{b.tag}`]({b.url}) (`{b.commit[:12]}`).", "",  # type: ignore[index]
                f"Upstream changes: {b.compare_url}", ""]
    if b.cuda_tag:
        out += [f"Sets the `cuda` row of `images.json` to `{b.cuda_tag}` (`{b.cuda_digest}`), "
                f"the newest `server-cuda-b<M>` ggml-org has published with M at or below the pinned release "
                f"(`{b.release}`); it was `{b.cuda_previous}`. ggml-org skips releases, so it can lag the pin. "
                f"There is no hardware gate for it (the CUDA lane is hidden in hal0); hal0's own pin for cuda "
                f"moves through the Promote runner image workflow.", ""]
    out += ["Files changed: " + ", ".join(f"`{f}`" for f in b.files) + ".", ""]
    if b.tag:
        out += ["Not changed here, for a person to do in this PR:", "",
                "- [ ] the `#:` notes above `version` in `runners/llama.cpp.toml`, which describe the old release",
                "- [ ] `[verification]` probes that should change for this release (read the upstream diff)", "",
                "## Before merging", "",
                "- [ ] Read the upstream diff for changes to flags, defaults, API or chat templating",
                "- [ ] `build.yml` is green: it builds the changed images with `push=false`", ""]
    out += ["## After merging", "",
            "Merging builds and pushes the immutable tags (if a runner changed) and posts the candidate digests on "
            "the merge commit. Then run the hardware gate on each digest and promote the llama.cpp group in one "
            "Promote runner image run (docs/UPDATING.md). Until a promote PR lands, nothing that hal0 pulls changes.", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="rewrite the pin file and the cuda row")
    ap.add_argument("--json-file", default="")
    ap.add_argument("--body-file", default="", help="write the bump PR description here")
    args = ap.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    bump = run(lib.ROOT, lambda p: lib.github_get(p, token), args.apply)
    result = {"bump": asdict(bump) if bump else None}
    print(json.dumps(result, indent=2))
    if args.body_file and bump:
        Path(args.body_file).write_text(pr_body(bump))
    if args.json_file:
        Path(args.json_file).write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
