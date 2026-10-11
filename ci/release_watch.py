#!/usr/bin/env python3
"""Weekly upstream watch: find newer releases, and prepare ONE llama.cpp bump.

    python3 ci/release_watch.py --report-file report.md --json-file result.json
    python3 ci/release_watch.py --apply ...   # also rewrite the files for the bump

What it does
  * AUTO_BUMP runners (standard, cpu) pin the same ggml-org/llama.cpp release.
    If a newer release exists, --apply edits runners/<key>/manifest.toml
    ([source].version and [source].ref, plus ref_description when present) and
    the matching ARG lines in runners/<key>/Containerfile together, for every
    AUTO_BUMP runner, so one PR moves them as one.
  * Every other runner (flm, comfyui, kokoro, moonshine, qwen3tts, ...) is
    report-only: the report lists the newer upstream version, nothing is edited.

It builds nothing, pushes nothing and calls only the public GitHub API. The
workflow turns result.json into a PR (bump) and a tracking issue (report).

ggml-org tags every `b<N>` release as a *prerelease*, so GET /releases/latest
returns an old, unrelated release (v0.6.0 on 2026-10-11). For llama.cpp we list
releases and take the highest `b<N>` instead (UPSTREAM_RULES).
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

# Runners that follow the shared llama.cpp release pin and are bumped together.
AUTO_BUMP = ("standard", "cpu")

# Per-upstream release selection. Anything not listed uses /releases/latest and
# falls back to the newest tag.
UPSTREAM_RULES: dict[str, dict] = {
    "ggml-org/llama.cpp": {"tag_re": r"^b\d+$", "prereleases": True},
}

SHA40 = re.compile(r"^[0-9a-f]{40}$")

Api = Callable[[str], object]


@dataclass
class Upstream:
    tag: str
    url: str
    name: str = ""
    published: str = ""


@dataclass
class Row:
    key: str
    current: str
    upstream_repo: str
    latest: str = ""
    latest_url: str = ""
    status: str = ""  # current | newer | unchecked
    note: str = ""


@dataclass
class Bump:
    previous: dict  # key -> old version
    tag: str
    commit: str
    url: str
    compare_url: str
    runners: list[str]
    files: list[str] = field(default_factory=list)


def version_key(tag: str) -> tuple[int, ...] | None:
    """Comparable key from a tag: 'b11552' -> (11552,), 'v0.9.44' -> (0, 9, 44)."""
    nums = re.findall(r"\d+", tag)
    return tuple(int(n) for n in nums) if nums else None


def is_newer(candidate: str, current: str) -> bool:
    a, b = version_key(candidate), version_key(current)
    return a is not None and b is not None and a > b


def same_version(a: str, b: str) -> bool:
    return a.lstrip("vV") == b.lstrip("vV")


def latest_upstream(owner_repo: str, api: Api) -> Upstream | None:
    rule = UPSTREAM_RULES.get(owner_repo)
    if rule:
        rels = api(f"/repos/{owner_repo}/releases?per_page=100") or []
        pat = re.compile(rule["tag_re"])
        ok = [
            r for r in rels
            if not r.get("draft")
            and (rule.get("prereleases") or not r.get("prerelease"))
            and pat.match(r.get("tag_name", ""))
        ]
        if not ok:
            return None
        best = max(ok, key=lambda r: version_key(r["tag_name"]) or (0,))
        return Upstream(best["tag_name"], best["html_url"], best.get("name") or "", best.get("published_at") or "")
    rel = api(f"/repos/{owner_repo}/releases/latest")
    if rel:
        return Upstream(rel["tag_name"], rel["html_url"], rel.get("name") or "", rel.get("published_at") or "")
    tags = api(f"/repos/{owner_repo}/tags?per_page=30") or []
    tags = [t for t in tags if version_key(t.get("name", ""))]
    if not tags:
        return None
    best = max(tags, key=lambda t: version_key(t["name"]) or (0,))
    return Upstream(best["name"], f"https://github.com/{owner_repo}/releases/tag/{best['name']}")


def resolve_commit(owner_repo: str, tag: str, api: Api) -> str:
    data = api(f"/repos/{owner_repo}/commits/{tag}")
    sha = (data or {}).get("sha", "")
    if not SHA40.match(sha):
        raise lib.CIError(f"could not resolve {owner_repo}@{tag} to a commit")
    return sha


# ---- file rewriting -------------------------------------------------------

_KV = r'^(\s*{key}\s*=\s*)(["\'])(.*?)\2(.*)$'


def rewrite_source_table(text: str, new_version: str, new_ref: str, new_desc: str | None) -> tuple[str, int]:
    """Set version/ref (and ref_description if present) inside [source] only."""
    out, in_source, changed = [], False, 0
    values = {"version": new_version, "ref": new_ref}
    if new_desc is not None:
        values["ref_description"] = new_desc
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            in_source = stripped.split("#")[0].strip() == "[source]"
        elif in_source:
            for key, val in values.items():
                m = re.match(_KV.format(key=key), line.rstrip("\n"))
                if m:
                    nl = "\n" if line.endswith("\n") else ""
                    line = f"{m.group(1)}{m.group(2)}{val}{m.group(2)}{m.group(4)}{nl}"
                    changed += 1
                    break
        out.append(line)
    return "".join(out), changed


def rewrite_containerfile_args(text: str, replacements: dict[str, str]) -> tuple[str, int]:
    """Replace `ARG NAME=<old>` values equal to a key of `replacements`."""
    changed = 0
    lines = []
    for line in text.splitlines(keepends=True):
        m = re.match(r'^(\s*ARG\s+\w+\s*=\s*)(["\']?)([^"\'\s#]+)\2(\s*(?:#.*)?)(\r?\n?)$', line)
        if m and m.group(3) in replacements:
            line = f"{m.group(1)}{m.group(2)}{replacements[m.group(3)]}{m.group(2)}{m.group(4)}{m.group(5)}"
            changed += 1
        lines.append(line)
    return "".join(lines), changed


def apply_bump(key: str, new_tag: str, new_commit: str, desc: str, root: Path = lib.ROOT) -> list[str]:
    """Edit manifest.toml and Containerfile for one runner. Returns touched paths."""
    manifest_path = root / "runners" / key / "manifest.toml"
    manifest = lib.load_manifest(key, root)
    src = lib.source_of(key, manifest)
    old_version, old_ref = src["version"], src["ref"]
    # Same style the recipe already uses: a commit SHA stays a SHA, a tag stays a tag.
    new_ref = new_commit if SHA40.match(old_ref) else new_tag

    text = manifest_path.read_text()
    new_text, n = rewrite_source_table(
        text, new_tag, new_ref, desc if "ref_description" in src else None
    )
    if n < 2:
        raise lib.CIError(f"runners/{key}/manifest.toml: could not rewrite [source].version and [source].ref")
    parsed = tomllib.loads(new_text)["source"]
    if parsed["version"] != new_tag or parsed["ref"] != new_ref:
        raise lib.CIError(f"runners/{key}/manifest.toml: rewrite produced {parsed!r}")
    manifest_path.write_text(new_text)

    _, containerfile = lib.build_paths(key, manifest, root)
    cf_path = root / containerfile
    repl = {old_ref: new_ref, old_version: new_tag}
    new_cf, n_cf = rewrite_containerfile_args(cf_path.read_text(), repl)
    if n_cf == 0:
        raise lib.CIError(
            f"{containerfile}: no `ARG <NAME>={old_ref}` or `ARG <NAME>={old_version}` line to bump; "
            "the pin must live in an ARG default (see ci/lib.py recipe contract)"
        )
    cf_path.write_text(new_cf)
    return [f"runners/{key}/manifest.toml", containerfile]


# ---- the watch ------------------------------------------------------------

def scan(root: Path, api: Api) -> tuple[list[Row], dict[str, Upstream | None]]:
    rows: list[Row] = []
    cache: dict[str, Upstream | None] = {}
    for key in lib.runner_keys(root):
        src = lib.source_of(key, lib.load_manifest(key, root))
        gh = lib.split_github_repo(src["repo"])
        row = Row(key=key, current=src["version"], upstream_repo=src["repo"])
        if not gh:
            row.status, row.note = "unchecked", "upstream is not a github.com repository"
            rows.append(row)
            continue
        slug = f"{gh[0]}/{gh[1]}"
        if slug not in cache:
            cache[slug] = latest_upstream(slug, api)
        up = cache[slug]
        if not up:
            row.status, row.note = "unchecked", "no release or tag found upstream"
        else:
            row.latest, row.latest_url = up.tag, up.url
            if same_version(up.tag, src["version"]):
                row.status = "current"
            elif is_newer(up.tag, src["version"]):
                row.status = "newer"
            else:
                row.status = "unchecked"
                row.note = "latest upstream release is not newer than the pin, or the version formats differ; check by hand"
        rows.append(row)
    return rows, cache


def plan_bump(rows: list[Row], root: Path, api: Api) -> Bump | None:
    cands = [r for r in rows if r.key in AUTO_BUMP and r.status == "newer"]
    if not cands:
        return None
    tags = {r.latest for r in cands}
    if len(tags) != 1:
        raise lib.CIError(f"AUTO_BUMP runners disagree on the latest upstream tag: {sorted(tags)}")
    # Bump every AUTO_BUMP runner that exists, so they never drift apart.
    runners = [r.key for r in rows if r.key in AUTO_BUMP and r.status in ("newer", "current")]
    tag = cands[0].latest
    gh = lib.split_github_repo(cands[0].upstream_repo)
    assert gh
    slug = f"{gh[0]}/{gh[1]}"
    commit = resolve_commit(slug, tag, api)
    previous = {r.key: r.current for r in rows if r.key in runners}
    oldest = min(previous.values(), key=lambda v: version_key(v) or (0,))
    return Bump(
        previous=previous, tag=tag, commit=commit, url=cands[0].latest_url,
        compare_url=f"https://github.com/{slug}/compare/{oldest}...{tag}", runners=runners,
    )


def render_report(rows: list[Row], bump: Bump | None) -> str:
    out = ["# Upstream release watch", ""]
    if bump:
        out += [f"A bump PR for **{', '.join(bump.runners)}** to `{bump.tag}` is being prepared ({bump.url}).", ""]
    out += ["| runner | pinned | latest upstream | status |", "|---|---|---|---|"]
    for r in rows:
        latest = f"[{r.latest}]({r.latest_url})" if r.latest else "-"
        status = {"current": "up to date", "newer": "newer upstream", "unchecked": f"unchecked: {r.note}"}[r.status]
        if r.status == "newer" and r.key in AUTO_BUMP:
            status = "newer upstream (bump PR)"
        elif r.status == "newer":
            status = "newer upstream (report only: bump by hand, see docs/UPDATING.md)"
        out.append(f"| `{r.key}` | `{r.current}` | {latest} | {status} |")
    out.append("")
    return "\n".join(out)


def run(root: Path, api: Api, apply: bool) -> tuple[list[Row], Bump | None, str]:
    rows, _ = scan(root, api)
    bump = plan_bump(rows, root, api)
    if bump and apply:
        desc = f"llama.cpp release {bump.tag}"
        for key in bump.runners:
            bump.files += apply_bump(key, bump.tag, bump.commit, desc, root)
    return rows, bump, render_report(rows, bump)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="rewrite the recipe files for the bump")
    ap.add_argument("--report-file", default="")
    ap.add_argument("--json-file", default="")
    args = ap.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    rows, bump, report = run(lib.ROOT, lambda p: lib.github_get(p, token), args.apply)
    print(report)
    if args.report_file:
        Path(args.report_file).write_text(report)
    if args.json_file:
        Path(args.json_file).write_text(json.dumps(
            {"bump": asdict(bump) if bump else None, "rows": [asdict(r) for r in rows]}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
