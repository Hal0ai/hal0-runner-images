#!/usr/bin/env python3
"""Decide which runners a build.yml run builds, and emit the matrix.

    python3 ci/plan.py --sha <sha> --base <sha> --head <sha>   # pull_request / push
    python3 ci/plan.py --sha <sha> --select standard,cpu       # workflow_dispatch
    python3 ci/plan.py --sha <sha> --select all

A runner is selected when a changed path is under runners/<key>/. A change under
runners/_shared/ selects only the runners that mention "_shared" in their own
files (the Containerfile COPY line, a build script). A base that is empty, all
zeros or unknown to git selects every runner: fail wide, never build nothing by
accident.

Outputs (GITHUB_OUTPUT): matrix, has_runners, runners.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

ZERO_SHA = "0" * 40
SHARED = "_shared"
# Files the "mentions _shared" scan reads. Small text recipe files only.
SCAN_MAX_BYTES = 1_000_000


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


def uses_shared(key: str, root: Path = lib.ROOT) -> bool:
    for p in (root / "runners" / key).rglob("*"):
        if "patches" in p.parts or not p.is_file():
            continue
        try:
            if p.stat().st_size <= SCAN_MAX_BYTES and SHARED in p.read_text(errors="ignore"):
                return True
        except OSError:
            continue
    return False


def select_changed(keys: list[str], files: list[str] | None, root: Path = lib.ROOT) -> list[str]:
    if files is None:
        return list(keys)
    hit: set[str] = set()
    shared_changed = False
    for f in files:
        parts = f.split("/")
        if len(parts) < 3 or parts[0] != "runners":  # runners/README.md etc. are not recipes
            continue
        if parts[1] == SHARED:
            shared_changed = True
        elif parts[1] in keys:
            hit.add(parts[1])
    if shared_changed:
        hit.update(k for k in keys if uses_shared(k, root))
    return sorted(hit)


def select_explicit(keys: list[str], select: str) -> list[str]:
    wanted = [s.strip() for s in select.split(",") if s.strip()]
    if wanted == ["all"]:
        return list(keys)
    unknown = [w for w in wanted if w not in keys]
    if unknown:
        raise lib.CIError(f"unknown runner(s) {unknown}; known: {keys}")
    return sorted(set(wanted))


def build_matrix(selected: list[str], sha: str, root: Path = lib.ROOT) -> dict:
    legs = []
    for key in selected:
        manifest = lib.load_manifest(key, root)
        src = lib.source_of(key, manifest)
        context, containerfile = lib.build_paths(key, manifest, root)
        legs.append({
            "key": key,
            "package": lib.package_for(key),
            "version": src["version"],
            "tag": lib.immutable_tag(src["version"], sha),
            "upstream_repo": src["repo"],
            "upstream_ref": src["ref"],
            "context": context,
            "containerfile": containerfile,
            "has_check": (root / "runners" / key / "build.sh").is_file(),
        })
    return {"include": legs}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sha", required=True, help="commit the tag's -r<sha7> comes from")
    ap.add_argument("--base", default="")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--select", default="", help="comma list of runner keys, or 'all' (workflow_dispatch)")
    args = ap.parse_args(argv)

    keys = lib.runner_keys()
    if args.select:
        selected = select_explicit(keys, args.select)
    else:
        selected = select_changed(keys, changed_files(args.base, args.head))
    matrix = build_matrix(selected, args.sha)

    print(f"runners selected: {selected or 'none'}", file=sys.stderr)
    lib.set_output("matrix", json.dumps(matrix, separators=(",", ":")))
    lib.set_output("has_runners", "true" if selected else "false")
    lib.set_output("runners", ",".join(selected))
    return 0


if __name__ == "__main__":
    sys.exit(main())
