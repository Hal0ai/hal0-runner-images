"""Shared helpers for the CI scripts (plan, release_watch, promote).

stdlib only: the workflows call these on a bare GitHub-hosted runner with no
pip install step.

THE RECIPE CONTRACT the CI relies on (the recipes side owns these files; if one
stops holding, adjust it here and in docs/UPDATING.md):

  runners/<key>/manifest.toml   one directory per runner; the name is the key.
  scripts/runner.py             the only build entrypoint:
      list --only k1,k2         GitHub matrix JSON: key, image, tag, heavy, timeout_minutes
      build <key> --check       static checks, no container runtime
      build <key> --tag <ref>   build (never pushes); sets every OCI label, and
                                org.opencontainers.image.version must equal the tag
  runners/llama.cpp.toml        the ONE llama.cpp release pin shared by standard,
                                cpu (and the external cuda entry); see release_watch.py
  images.json                   hal0.runner-images.v2, runners[] keyed by `id`
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REPO_SLUG = "Hal0ai/hal0-runner-images"
REPO_URL = f"https://github.com/{REPO_SLUG}"
PACKAGE_PREFIX = "ghcr.io/hal0ai/hal0-runner-"

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
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


def package_for(key: str) -> str:
    return f"{PACKAGE_PREFIX}{key}"


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
