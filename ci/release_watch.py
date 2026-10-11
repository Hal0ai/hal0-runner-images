#!/usr/bin/env python3
"""Prepare ONE llama.cpp bump when ggml-org has a newer release.

    python3 ci/release_watch.py --json-file result.json            # report only
    python3 ci/release_watch.py --apply --json-file result.json    # also rewrite files

standard, cpu (and the external cuda entry) share one llama.cpp release pin,
PIN_FILE. --apply rewrites that file, and wherever the old release is mirrored
(Containerfile `ARG NAME=<old>` defaults, `[source]` tables of runner manifests),
so one PR moves everything together. The other runners are report-only; that is
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
# THE ONE MAPPING to adjust if the recipes side moves the shared pin.
PIN_FILE = "runners/llama.cpp.toml"
PIN_TABLE = "llama_cpp"                 # [llama_cpp]
PIN_VERSION_KEY = "version"             # release tag, e.g. "b11552"
PIN_REF_KEY = "ref"                     # full commit sha of that tag
PIN_DESC_KEY = "ref_description"        # rewritten when present
UPSTREAM = "ggml-org/llama.cpp"
TAG_RE = r"^b\d+$"
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


def rewrite_table(text: str, table: str, values: dict[str, str]) -> tuple[str, int]:
    """Set the given string keys inside [table] only. Returns (text, keys changed)."""
    out, inside, changed = [], False, 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            inside = stripped.split("#")[0].strip() == f"[{table}]"
        elif inside:
            for key, val in values.items():
                m = _kv_re(key).match(line.rstrip("\r\n"))
                if m:
                    nl = line[len(line.rstrip("\r\n")):]
                    line = f"{m.group(1)}{m.group(2)}{val}{m.group(2)}{m.group(4)}{nl}"
                    changed += 1
                    break
        out.append(line)
    return "".join(out), changed


def rewrite_containerfile_args(text: str, replacements: dict[str, str]) -> tuple[str, int]:
    """Replace `ARG NAME=<old>` values that equal a key of `replacements`."""
    changed, lines = 0, []
    for line in text.splitlines(keepends=True):
        m = re.match(r'^(\s*ARG\s+\w+\s*=\s*)(["\']?)([^"\'\s#]+)\2(\s*(?:#.*)?)(\r?\n?)$', line)
        if m and m.group(3) in replacements:
            line = f"{m.group(1)}{m.group(2)}{replacements[m.group(3)]}{m.group(2)}{m.group(4)}{m.group(5)}"
            changed += 1
        lines.append(line)
    return "".join(lines), changed


def read_pin(root: Path) -> dict:
    path = root / PIN_FILE
    if not path.is_file():
        raise lib.CIError(f"{PIN_FILE} does not exist")
    table = tomllib.loads(path.read_text()).get(PIN_TABLE) or {}
    for k in (PIN_VERSION_KEY, PIN_REF_KEY):
        if not isinstance(table.get(k), str) or not table[k]:
            raise lib.CIError(f"{PIN_FILE}: [{PIN_TABLE}].{k} missing or empty")
    return table


def apply_bump(root: Path, old_version: str, old_ref: str, new_tag: str, new_commit: str) -> list[str]:
    """Rewrite the pin file and every mirror of the old pin. Returns touched paths."""
    touched: list[str] = []
    pin_path = root / PIN_FILE
    values = {PIN_VERSION_KEY: new_tag, PIN_REF_KEY: new_commit, PIN_DESC_KEY: f"llama.cpp release {new_tag}"}
    text, n = rewrite_table(pin_path.read_text(), PIN_TABLE, values)
    if n < 2:
        raise lib.CIError(f"{PIN_FILE}: could not rewrite [{PIN_TABLE}].{PIN_VERSION_KEY}/{PIN_REF_KEY}")
    parsed = tomllib.loads(text)[PIN_TABLE]
    if parsed[PIN_VERSION_KEY] != new_tag or parsed[PIN_REF_KEY] != new_commit:
        raise lib.CIError(f"{PIN_FILE}: rewrite produced {parsed!r}")
    pin_path.write_text(text)
    touched.append(PIN_FILE)

    # Mirrors. The ref follows the old ref's style: a SHA stays a SHA, a tag stays a tag.
    repl = {old_version: new_tag, old_ref: new_commit if SHA40.match(old_ref) else new_tag}
    for d in sorted((root / "runners").iterdir()) if (root / "runners").is_dir() else []:
        if not d.is_dir() or d.name.startswith("_"):
            continue
        cf = d / "Containerfile"
        if cf.is_file():
            new_cf, n_cf = rewrite_containerfile_args(cf.read_text(), repl)
            if n_cf:
                cf.write_text(new_cf)
                touched.append(str(cf.relative_to(root)))
        mf = d / "manifest.toml"
        if mf.is_file():
            src = tomllib.loads(mf.read_text()).get("source") or {}
            vals = {k: repl[src[k]] for k in ("version", "ref") if src.get(k) in repl}
            if vals:
                new_mf, n_mf = rewrite_table(mf.read_text(), "source", vals)
                if n_mf:
                    mf.write_text(new_mf)
                    touched.append(str(mf.relative_to(root)))
    return touched


def run(root: Path, api: Api, apply: bool) -> Bump | None:
    pin = read_pin(root)
    rel = latest_release(api)
    if not rel:
        raise lib.CIError(f"no {TAG_RE} release found for {UPSTREAM}")
    if not is_newer(rel["tag_name"], pin[PIN_VERSION_KEY]):
        return None
    tag = rel["tag_name"]
    bump = Bump(
        previous=pin[PIN_VERSION_KEY], tag=tag, commit=resolve_commit(tag, api), url=rel["html_url"],
        compare_url=f"https://github.com/{UPSTREAM}/compare/{pin[PIN_VERSION_KEY]}...{tag}",
    )
    if apply:
        bump.files = apply_bump(root, pin[PIN_VERSION_KEY], pin[PIN_REF_KEY], tag, bump.commit)
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
