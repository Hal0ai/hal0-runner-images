#!/usr/bin/env python3
"""Prepare ONE llama.cpp bump when ggml-org has a newer release.

    python3 ci/release_watch.py --json-file result.json            # report only
    python3 ci/release_watch.py --apply --json-file result.json    # also rewrite files

`standard` and `cpu` pin the same llama.cpp release, each in its own
manifest.toml ([image].version, [source].release, [source].ref). --apply moves
both together, so one PR bumps them as one:

  [image].version   the release tag, e.g. b11552 (first half of the image tag)
  [source].release  the release tag
  [source].ref      the commit the tag points at (full SHA)
  [verification]    a probe sentence "release <old>" becomes "release <new>"

Comments, patches, probes that merely mention the old release (a record of what
failed at b11510, say) and the cuda row of images.json are NOT touched: the PR
body lists them for a person. The other runners are report-only; that is
scripts/source_watch.py, which the workflow runs next to this.

It builds nothing, pushes nothing and calls only the public GitHub API.

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
AUTO_BUMP = ("standard", "cpu")          # runners that share one llama.cpp release
LEAD = "standard"                        # the runner whose manifest says what is pinned now
UPSTREAM = "ggml-org/llama.cpp"
TAG_RE = r"^b\d+$"
# manifest table -> key -> what to write ("release" tag or "commit" sha)
MANIFEST_KEYS = {
    "image": {"version": "release"},
    "source": {"release": "release", "ref": "commit"},
}
# ---------------------------------------------------------------------------

SHA40 = re.compile(r"^[0-9a-f]{40}$")
Api = Callable[[str], object]


@dataclass
class Bump:
    previous: str
    tag: str
    commit: str
    url: str
    compare_url: str
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


def resolve_commit(tag: str, api: Api) -> str:
    sha = (api(f"/repos/{UPSTREAM}/commits/{tag}") or {}).get("sha", "")
    if not SHA40.match(sha):
        raise lib.CIError(f"could not resolve {UPSTREAM}@{tag} to a commit")
    return sha


# ---- file rewriting -------------------------------------------------------

def _kv_re(key: str) -> re.Pattern:
    return re.compile(r'^(\s*' + re.escape(key) + r'\s*=\s*)(["\'])(.*?)\2(.*)$')


def rewrite_manifest(text: str, release: str, commit: str, old_release: str) -> tuple[str, int]:
    """Set the pin keys (MANIFEST_KEYS) and the "release <old>" verification probes.

    Returns (new text, number of lines changed). Comment lines are never touched.
    """
    values = {"release": release, "commit": commit}
    out, table, changed = [], "", 0
    probe = re.compile(r"(?<![A-Za-z0-9])release " + re.escape(old_release) + r"(?![A-Za-z0-9])")
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            table = stripped.split("#")[0].strip().strip("[]")
        elif stripped.startswith("#"):
            pass
        elif table in MANIFEST_KEYS:
            for key, what in MANIFEST_KEYS[table].items():
                m = _kv_re(key).match(line.rstrip("\r\n"))
                if m:
                    nl = line[len(line.rstrip("\r\n")):]
                    line = f"{m.group(1)}{m.group(2)}{values[what]}{m.group(2)}{m.group(4)}{nl}"
                    changed += 1
                    break
        elif table == "verification" and probe.search(line):
            line = probe.sub(f"release {release}", line)
            changed += 1
        out.append(line)
    return "".join(out), changed


def read_pin(root: Path, key: str = LEAD) -> dict:
    path = root / "runners" / key / "manifest.toml"
    if not path.is_file():
        raise lib.CIError(f"runners/{key}/manifest.toml does not exist")
    doc = tomllib.loads(path.read_text())
    release, ref = (doc.get("source") or {}).get("release"), (doc.get("source") or {}).get("ref")
    if not release or not ref:
        raise lib.CIError(f"runners/{key}/manifest.toml: [source].release and [source].ref are required")
    return {"release": release, "ref": ref}


def apply_bump(root: Path, old_release: str, new_tag: str, new_commit: str) -> list[str]:
    """Rewrite every AUTO_BUMP manifest. Returns touched paths."""
    touched: list[str] = []
    for key in AUTO_BUMP:
        path = root / "runners" / key / "manifest.toml"
        if not path.is_file():
            continue
        text, n = rewrite_manifest(path.read_text(), new_tag, new_commit, old_release)
        doc = tomllib.loads(text)
        if (doc["image"]["version"], doc["source"]["release"], doc["source"]["ref"]) != (new_tag, new_tag, new_commit) or n < 3:
            raise lib.CIError(f"runners/{key}/manifest.toml: could not rewrite the pin ({n} lines changed)")
        path.write_text(text)
        touched.append(f"runners/{key}/manifest.toml")
    return touched


def run(root: Path, api: Api, apply: bool) -> Bump | None:
    pin = read_pin(root)
    rel = latest_release(api)
    if not rel:
        raise lib.CIError(f"no {TAG_RE} release found for {UPSTREAM}")
    if not is_newer(rel["tag_name"], pin["release"]):
        return None
    tag = rel["tag_name"]
    bump = Bump(
        previous=pin["release"], tag=tag, commit=resolve_commit(tag, api), url=rel["html_url"],
        compare_url=f"https://github.com/{UPSTREAM}/compare/{pin['release']}...{tag}",
    )
    if apply:
        bump.files = apply_bump(root, pin["release"], tag, bump.commit)
    return bump


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="rewrite the pin file and its mirrors")
    ap.add_argument("--json-file", default="")
    args = ap.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    bump = run(lib.ROOT, lambda p: lib.github_get(p, token), args.apply)
    result = {"bump": asdict(bump) if bump else None}
    print(json.dumps(result, indent=2))
    if args.json_file:
        Path(args.json_file).write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
