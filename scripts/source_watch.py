#!/usr/bin/env python3
"""Release watch: has upstream published something newer than each runner pins?

    python3 scripts/source_watch.py            # needs network; GITHUB_TOKEN raises the rate limit
    python3 scripts/source_watch.py --offline  # list what would be checked, no network

For every runners/<key>/manifest.toml, read [lifecycle.track] and ask
upstream for its newest release:

  kind = "github-release"  newest non-draft GitHub release of [source].repo
                           whose tag matches `pattern`; releases marked
                           prerelease count only with include_prereleases
                           (compared with [source].release, or with
                           [image].version when the pin sits between releases)
  kind = "pypi"            newest version of [source].package on PyPI
                           (compared with [source].version)

Runners whose [source].pin names a shared pin file (standard and cpu use
runners/llama.cpp.toml, which the external cuda entry follows too) are
reported once, as that file; the release watch bumps it in one pull request.

For git sources it also checks that the pinned commit still exists. It
prints one markdown report and changes nothing; a person decides which
bumps to make (docs/UPDATING.md).

Alerts are the cases that need attention now, not merely an update:
  * the pinned commit is gone upstream (history rewritten or repo deleted),
    so the recipe cannot be rebuilt until the pin moves;
  * no release matches the pattern (the pattern or the repo is wrong);
  * an upstream request failed in any way (HTTP error, DNS, connection or
    timeout), including the pinned-commit check. The report still renders
    and the script exits 0.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GH_REPO = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")
#: HTTP codes that mean "this object does not exist". Anything else that is
#: not 200 (403 rate limit or proxy, 5xx) is an unknown, never a verdict.
MISSING = {404, 422}


@dataclass
class Target:
    key: str
    kind: str          # "github-release" | "pypi"
    repo_url: str
    pinned: str        # release tag or PyPI version the recipe pins
    commit: str = ""   # pinned commit for git sources
    pattern: str = ""  # github-release tag pattern
    package: str = ""  # PyPI package
    prereleases: bool = False  # github-release: count releases marked prerelease
    followers: list[str] | None = None  # runners that follow this shared pin file

    @property
    def owner_repo(self) -> tuple[str, str]:
        m = GH_REPO.match(self.repo_url)
        if not m:
            raise SystemExit(f"runners/{self.key}: not a github.com repo URL: {self.repo_url}")
        return m.group(1), m.group(2)


def load_targets() -> list[Target]:
    out = []
    shared: dict[str, list[str]] = {}
    tracks: dict[str, dict] = {}
    for manifest in sorted((ROOT / "runners").glob("*/manifest.toml")):
        doc = tomllib.loads(manifest.read_text())
        key = manifest.parent.name
        src = doc["source"]
        track = doc["lifecycle"]["track"]
        if src.get("pin"):
            # Follows a shared pin file: reported once, under the file's name.
            shared.setdefault(src["pin"], []).append(key)
            tracks[src["pin"]] = track
            continue
        if track["kind"] == "github-release":
            # A commit pinned between releases (no [source].release) is
            # compared by the version it reports ([image].version).
            pinned = src.get("release") or doc["image"]["version"]
            out.append(Target(key, "github-release", src["repo"], pinned,
                              commit=src.get("ref", ""), pattern=track["pattern"],
                              prereleases=bool(track.get("include_prereleases"))))
        elif track["kind"] == "pypi":
            out.append(Target(key, "pypi", src["repo"], src["version"], package=src["package"]))
        else:
            raise SystemExit(f"runners/{key}: unknown [lifecycle.track].kind {track['kind']!r}")
    for pin, keys in sorted(shared.items()):
        table = tomllib.loads((ROOT / "runners" / pin).read_text())["llama_cpp"]
        track = tracks[pin]
        out.append(Target(pin, "github-release", table["repo"], table["version"], commit=table["ref"],
                          pattern=track["pattern"], prereleases=bool(track.get("include_prereleases")),
                          followers=keys + ["cuda"]))
    return sorted(out, key=lambda t: t.key)


@dataclass
class Row:
    target: Target
    latest: str
    state: str
    alert: bool


def newest_release(releases: list[dict], pattern: str, prereleases: bool = False) -> str | None:
    """Pure: the newest matching, published tag (prereleases only when asked)."""
    rx = re.compile(pattern)
    good = [r for r in releases
            if not r.get("draft") and (prereleases or not r.get("prerelease"))
            and rx.match(r.get("tag_name", ""))]
    if not good:
        return None
    return max(good, key=lambda r: r.get("published_at") or "")["tag_name"]


#: Status _get reports when no HTTP answer arrived at all (DNS failure,
#: refused connection, timeout, unreadable body).
NO_ANSWER = 0


def _answer(code: int) -> str:
    return "no answer (network error)" if code == NO_ANSWER else f"HTTP {code}"


def classify(t: Target, latest: str | None, api_code: int = 200, pin_code: int = 200) -> Row:
    """Pure: turn upstream answers into one report row. Fixture-tested.

    Anything but a clean 200 from every request is an alert: a failed probe
    is never read as "current".
    """
    if pin_code in MISSING:
        return Row(t, latest or "?", "pinned commit gone upstream; the recipe cannot be rebuilt until the pin moves", True)
    if api_code != 200:
        return Row(t, "?", f"upstream did not answer ({_answer(api_code)}); see the workflow log", True)
    if pin_code != 200:
        return Row(t, latest or "?",
                   f"could not check the pinned commit ({_answer(pin_code)}); see the workflow log", True)
    if latest is None:
        what = f"tag pattern `{t.pattern}`" if t.kind == "github-release" else "PyPI"
        return Row(t, "?", f"no release found ({what})", True)
    if latest.removeprefix("v") == t.pinned.removeprefix("v"):
        return Row(t, latest, "current", False)
    return Row(t, latest, "newer release available", False)


def _get(url: str) -> tuple[int, object | None]:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28",
                                               "User-Agent": "hal0-runner-images-source-watch"})
    tok = os.environ.get("GITHUB_TOKEN")
    if tok and url.startswith("https://api.github.com/"):
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        # DNS failure, refused connection, timeout (TimeoutError is an
        # OSError), a connection dropped mid-response (RemoteDisconnected), a
        # truncated body (http.client.IncompleteRead, an HTTPException rather
        # than an OSError) or a body that is not complete JSON: report it and
        # keep going, so one bad upstream never takes the whole report down.
        print(f"source_watch: {url}: {e!r}", file=sys.stderr)
        return NO_ANSWER, None


def probe(t: Target) -> Row:
    pin_code = 200
    if t.kind == "pypi":
        code, doc = _get(f"https://pypi.org/pypi/{t.package}/json")
        latest = doc["info"]["version"] if code == 200 and isinstance(doc, dict) else None
        return classify(t, latest, code)
    owner, repo = t.owner_repo
    api = f"https://api.github.com/repos/{owner}/{repo}"
    if t.commit:
        pin_code, _ = _get(f"{api}/commits/{t.commit}")
    code, releases = _get(f"{api}/releases?per_page=50")
    latest = newest_release(releases, t.pattern, t.prereleases) if code == 200 and isinstance(releases, list) else None
    return classify(t, latest, code, pin_code)


def render(rows: list[Row]) -> str:
    alerts = [r for r in rows if r.alert]
    updates = [r for r in rows if not r.alert and r.state != "current"]
    lines = [
        "Weekly check of every runner's pinned upstream release. Nothing is built from "
        "this report; a bump follows docs/UPDATING.md.",
        "",
        (f"**{len(alerts)} alert(s).**" if alerts else "**No alerts.**")
        + f" {len(updates)} runner(s) have a newer release.",
        "",
        "| runner | upstream | pinned | latest | state |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        t = r.target
        where = t.package if t.kind == "pypi" else "/".join(t.owner_repo)
        state = f"⚠️ {r.state}" if r.alert else r.state
        if t.followers and not r.alert and r.state != "current":
            state += " (bumped via llama.cpp.toml by the release watch)"
        name = f"`{t.key}` ({', '.join(t.followers)})" if t.followers else f"`{t.key}`"
        lines.append(f"| {name} | {where} ({t.kind}) | `{t.pinned}` | `{r.latest}` | {state} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    targets = load_targets()
    if "--offline" in argv:
        for t in targets:
            print(f"{t.key}\t{t.kind}\t{t.package or t.repo_url}\t{t.pinned}")
        return 0
    sys.stdout.write(render([probe(t) for t in targets]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
