#!/usr/bin/env python3
"""Decide which runners a build.yml run builds, and emit the matrix.

    python3 ci/plan.py --base <sha> --head <sha>     # pull_request / push
    python3 ci/plan.py --select standard,cpu         # workflow_dispatch
    python3 ci/plan.py --select all

A runner is selected when a changed path is under runners/<key>/. A changed file
directly under runners/ (runners/llama.cpp.toml, the shared llama.cpp pin) selects
the runners whose manifest.toml names it (`pin = "llama.cpp.toml"`). A base that is empty, all
zeros or unknown to git selects every runner: fail wide, never build nothing by
accident. The matrix itself (image, tag, heavy, timeout) comes from
`scripts/runner.py list`, so the recipes side stays the one authority.

Outputs (GITHUB_OUTPUT): matrix, has_runners, runners.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

ZERO_SHA = "0" * 40
REQUIRED_LEG_FIELDS = ("key", "image", "tag")


def changed_files(base: str, head: str, root: Path = lib.ROOT) -> list[str] | None:
    """Paths changed between two commits, or None when the diff cannot be taken."""
    if not base or base == ZERO_SHA:
        return None
    proc = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", base, head],
        cwd=root, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"::notice::git diff {base}..{head} failed ({proc.stderr.strip()}); building every runner")
        return None
    return [p for p in proc.stdout.splitlines() if p]


def names_file(key: str, name: str, root: Path = lib.ROOT) -> bool:
    manifest = root / "runners" / key / "manifest.toml"
    return manifest.is_file() and name in manifest.read_text(errors="ignore")


def select_changed(keys: list[str], files: list[str] | None, root: Path = lib.ROOT) -> list[str]:
    if files is None:
        return list(keys)
    hit: set[str] = set()
    for f in files:
        parts = f.split("/")
        if len(parts) >= 3 and parts[0] == "runners" and parts[1] in keys:
            hit.add(parts[1])
        elif len(parts) == 2 and parts[0] == "runners" and parts[1].endswith(".toml"):
            hit.update(k for k in keys if names_file(k, parts[1], root))
    return sorted(hit)


def select_explicit(keys: list[str], select: str) -> list[str]:
    wanted = [s.strip() for s in select.split(",") if s.strip()]
    if wanted == ["all"]:
        return list(keys)
    unknown = [w for w in wanted if w not in keys]
    if unknown:
        raise lib.CIError(f"unknown runner(s) {unknown}; known: {keys}")
    return sorted(set(wanted))


def runner_list(selected: list[str], root: Path = lib.ROOT) -> dict:
    proc = subprocess.run(
        [sys.executable, "scripts/runner.py", "list", "--only", ",".join(selected)],
        cwd=root, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise lib.CIError(f"scripts/runner.py list failed:\n{proc.stderr.strip()}")
    return json.loads(proc.stdout)


def build_matrix(selected: list[str], lister: Callable[[list[str]], dict] = runner_list) -> dict:
    if not selected:
        return {"include": []}
    matrix = lister(selected)
    legs = matrix.get("include") or []
    if sorted(leg.get("key") for leg in legs) != sorted(selected):
        raise lib.CIError(f"runner.py list returned {[l.get('key') for l in legs]}, expected {selected}")
    for leg in legs:
        missing = [f for f in REQUIRED_LEG_FIELDS if not leg.get(f)]
        if missing:
            raise lib.CIError(f"runner.py list: {leg.get('key')} lacks {missing}")
        if leg["image"] != lib.package_for(leg["key"]):
            raise lib.CIError(f"{leg['key']}: image {leg['image']!r} is not {lib.package_for(leg['key'])!r}")
        if not lib.TAG_RE.match(leg["tag"]):
            raise lib.CIError(f"{leg['key']}: tag {leg['tag']!r} is not <version>-r<sha7>")
        leg.setdefault("heavy", False)
        leg.setdefault("timeout_minutes", 360)
    return matrix


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--select", default="", help="comma list of runner keys, or 'all' (workflow_dispatch)")
    args = ap.parse_args(argv)

    keys = lib.runner_keys()
    if args.select:
        selected = select_explicit(keys, args.select)
    else:
        selected = select_changed(keys, changed_files(args.base, args.head))
    matrix = build_matrix(selected)

    print(f"runners selected: {selected or 'none'}", file=sys.stderr)
    lib.set_output("matrix", json.dumps(matrix, separators=(",", ":")))
    lib.set_output("has_runners", "true" if selected else "false")
    lib.set_output("runners", ",".join(selected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
