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
  runners/{standard,cpu}/manifest.toml
                                pin the same llama.cpp release: [image].version,
                                [source].release, [source].ref (see release_watch.py)
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


def ghcr_tags(image: str, timeout: int = 30) -> list[str]:
    """Every tag of a public ghcr.io image (anonymous pull token, paginated).

    The registry's tag order is not alphabetical, so the whole list is read.
    """
    repo = image.removeprefix("ghcr.io/")
    try:
        with urllib.request.urlopen(
            f"https://ghcr.io/token?service=ghcr.io&scope=repository:{repo}:pull", timeout=timeout
        ) as resp:
            token = json.loads(resp.read())["token"]
        url: str | None = f"https://ghcr.io/v2/{repo}/tags/list?n=1000"
        tags: list[str] = []
        while url:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                tags += json.loads(resp.read()).get("tags") or []
                m = re.search(r'<([^>]+)>;\s*rel="next"', resp.headers.get("link", ""))
                url = f"https://ghcr.io{m.group(1)}" if m else None
        return tags
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        raise CIError(f"could not list the tags of {image}: {e}")


def ghcr_digest(image: str, tag: str, timeout: int = 30) -> str:
    """The content digest a public ghcr.io tag points at right now."""
    repo = image.removeprefix("ghcr.io/")
    accept = ", ".join((
        "application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ))
    try:
        with urllib.request.urlopen(
            f"https://ghcr.io/token?service=ghcr.io&scope=repository:{repo}:pull", timeout=timeout
        ) as resp:
            token = json.loads(resp.read())["token"]
        req = urllib.request.Request(
            f"https://ghcr.io/v2/{repo}/manifests/{tag}", method="HEAD",
            headers={"Authorization": f"Bearer {token}", "Accept": accept},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            digest = resp.headers.get("Docker-Content-Digest", "")
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        raise CIError(f"could not resolve {image}:{tag}: {e}")
    if not DIGEST_RE.match(digest):
        raise CIError(f"{image}:{tag} gave no usable digest ({digest!r})")
    return digest


def dump_like(doc: object, raw: str) -> str:
    """Serialise `doc` the way the JSON text `raw` was written, so a diff shows only the edit."""
    original = json.loads(raw)
    for ensure_ascii in (False, True):
        for indent in (2, 4):
            if json.dumps(original, indent=indent, ensure_ascii=ensure_ascii) + "\n" == raw:
                return json.dumps(doc, indent=indent, ensure_ascii=ensure_ascii) + "\n"
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def set_output(name: str, value: str) -> None:
    """Write a step output (GITHUB_OUTPUT), or echo it when run by hand."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(f"{name}={value}")
        return
    delim = "EOF_" + os.urandom(6).hex()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}<<{delim}\n{value}\n{delim}\n")
