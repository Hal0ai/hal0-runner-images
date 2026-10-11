"""Shared helpers for the CI scripts (plan, release_watch, promote).

stdlib only: the workflows call these on a bare GitHub-hosted runner with no
pip install step.

THE RECIPE CONTRACT the CI relies on. The recipes side owns these files; if
one of these assumptions stops holding, this module is the one place to adjust:

  runners/<key>/manifest.toml   required. Directory name == runner key.
      [source]  repo     GitHub URL of the upstream project
                version  upstream version string; becomes the image tag prefix
                ref      git tag or full commit SHA that the Containerfile builds
      [build]   context        optional, default "runners/<key>"
                containerfile  optional, default "<context>/Containerfile"
  runners/<key>/Containerfile   required (path above). The pins it consumes
                are ARG defaults, so the bump PR edits manifest.toml and the
                ARG lines together.
  runners/<key>/build.sh        optional. If present, `build.sh --check` runs
                on every PR / merge before the image build.
  runners/_shared/              optional. Not a runner. A runner is rebuilt for
                a change here only if one of its own files mentions "_shared".
"""

from __future__ import annotations

import json
import os
import re
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REPO_SLUG = "Hal0ai/hal0-runner-images"
REPO_URL = f"https://github.com/{REPO_SLUG}"
PACKAGE_PREFIX = "ghcr.io/hal0ai/hal0-runner-"

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Docker tag charset, no leading '.' or '-'.
VERSION_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,100}$")
# <upstream-version>-r<recipe-sha7>
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,100}-r[0-9a-f]{7}$")
GH_REPO_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")


class CIError(SystemExit):
    """A failure the workflow should report plainly (no traceback)."""

    def __init__(self, msg: str) -> None:
        super().__init__(f"error: {msg}")


def runner_keys(root: Path = ROOT) -> list[str]:
    """Every runner = a runners/<key>/ directory holding a manifest.toml.

    Directories starting with "_" (runners/_shared) are not runners.
    """
    base = root / "runners"
    if not base.is_dir():
        return []
    return sorted(
        p.name
        for p in base.iterdir()
        if p.is_dir() and not p.name.startswith("_") and (p / "manifest.toml").is_file()
    )


def load_manifest(key: str, root: Path = ROOT) -> dict:
    if not KEY_RE.match(key):
        raise CIError(f"invalid runner key {key!r}")
    path = root / "runners" / key / "manifest.toml"
    if not path.is_file():
        raise CIError(f"unknown runner {key!r}: {path.relative_to(root)} does not exist")
    return tomllib.loads(path.read_text())


def package_for(key: str) -> str:
    return f"{PACKAGE_PREFIX}{key}"


def source_of(key: str, manifest: dict) -> dict:
    src = manifest.get("source") or {}
    for field in ("repo", "version", "ref"):
        if not isinstance(src.get(field), str) or not src[field].strip():
            raise CIError(f"runners/{key}/manifest.toml: [source].{field} missing or empty")
    if not VERSION_RE.match(src["version"]):
        raise CIError(
            f"runners/{key}/manifest.toml: [source].version {src['version']!r} cannot be an "
            "image tag prefix (allowed: letters, digits, '_', '.', '-')"
        )
    return src


def immutable_tag(version: str, commit_sha: str) -> str:
    """`<upstream-version>-r<recipe-sha7>`. Built once, never moved."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit_sha):
        raise CIError(f"not a commit sha: {commit_sha!r}")
    return f"{version}-r{commit_sha[:7]}"


def build_paths(key: str, manifest: dict, root: Path = ROOT) -> tuple[str, str]:
    """(context, containerfile), both repo-relative and verified to exist."""
    build = manifest.get("build") or {}
    context = build.get("context") or f"runners/{key}"
    containerfile = build.get("containerfile") or f"{context}/Containerfile"
    for label, rel in (("context", context), ("containerfile", containerfile)):
        resolved = (root / rel).resolve()
        if root.resolve() not in resolved.parents and resolved != root.resolve():
            raise CIError(f"runners/{key}: [build].{label} {rel!r} escapes the repository")
    if not (root / context).is_dir():
        raise CIError(f"runners/{key}: build context {context!r} is not a directory")
    if not (root / containerfile).is_file():
        raise CIError(f"runners/{key}: {containerfile!r} does not exist")
    return context, containerfile


def split_github_repo(url: str) -> tuple[str, str] | None:
    m = GH_REPO_RE.match(url.strip())
    return (m.group(1), m.group(2)) if m else None


def github_get(path: str, token: str | None = None, timeout: int = 30):
    """GET https://api.github.com<path> and return parsed JSON (None on 404)."""
    url = path if path.startswith("https://") else f"https://api.github.com{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hal0-runner-images-ci",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise CIError(f"GitHub API {url}: HTTP {e.code} {e.reason}")
    except urllib.error.URLError as e:
        raise CIError(f"GitHub API {url}: {e.reason}")


def set_output(name: str, value: str) -> None:
    """Write a step output (GITHUB_OUTPUT), or echo it when run by hand."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(f"{name}={value}")
        return
    delim = "EOF_" + os.urandom(6).hex()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}<<{delim}\n{value}\n{delim}\n")
