#!/usr/bin/env python3
"""Weekly fork watch: is each pinned source still where we left it?

    python3 scripts/fork_watch.py            # needs GITHUB_TOKEN (or anonymous, rate-limited)
    python3 scripts/fork_watch.py --offline  # list targets only, no network

For every runners/*/manifest.toml ([source].ref against [lifecycle].track_ref)
and every build-recipes.json entry (llama_ref against track_ref), ask the
GitHub compare API how the pinned commit relates to the tracked branch and
print one markdown report. It builds nothing and changes nothing: the
workflow posts the report on a single tracking issue, and a person decides
which bumps are worth a build (docs/BUMPING.md).

Alerts are the cases where a pin is at risk, not merely old:
  * the pinned commit is gone from the repo (history rewritten or repo
    deleted) -> rebuild from the source mirror (docs/BUMPING.md);
  * the branch no longer contains the pin (force-pushed / diverged);
  * the tracked branch is gone.
"""

from __future__ import annotations

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
RUNNERS = ("rocmfpx", "upstream", "strix", "promptforge")
GH_REPO = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")


@dataclass
class Target:
    name: str
    repo_url: str
    pin: str
    track_ref: str
    retire_when: str = ""

    @property
    def owner_repo(self) -> tuple[str, str]:
        m = GH_REPO.match(self.repo_url)
        if not m:
            raise SystemExit(f"{self.name}: not a github.com repo URL: {self.repo_url}")
        return m.group(1), m.group(2)

    @property
    def branch(self) -> str:
        return self.track_ref.removeprefix("refs/heads/")


def load_targets() -> list[Target]:
    out = []
    for d in RUNNERS:
        doc = tomllib.loads((ROOT / "runners" / d / "manifest.toml").read_text())
        life = doc["lifecycle"]
        out.append(Target(f"runners/{d}", doc["source"]["repo"], doc["source"]["ref"],
                          life["track_ref"], life["retire_when"]))
    for r in json.loads((ROOT / "build-recipes.json").read_text())["recipes"]:
        out.append(Target(r["id"], r["llama_repo"], r["llama_ref"], r["track_ref"]))
    return out


@dataclass
class Row:
    target: Target
    state: str
    alert: bool
    detail: str
    link: str = ""


# HTTP codes that mean "this object does not exist". Anything else that is
# not 200 (403 rate limit or proxy, 5xx) is an unknown, never a verdict.
MISSING = {404, 422}


def classify(t: Target, compare: dict | None, pin_code: int | None = None,
             branch_code: int | None = None) -> Row:
    """Pure: turn API answers into one report row. Fixture-tested."""
    if compare is None:
        if pin_code in MISSING:
            return Row(t, "pin gone", True,
                       "pinned commit no longer exists upstream; rebuild only from the source mirror")
        if branch_code in MISSING:
            return Row(t, "branch gone", True, f"tracked branch `{t.branch}` no longer exists")
        return Row(t, "unknown", True,
                   f"GitHub API did not answer (commit HTTP {pin_code}, branch HTTP {branch_code}); "
                   "see the workflow log")
    status = compare.get("status")
    ahead = int(compare.get("ahead_by", 0))
    behind = int(compare.get("behind_by", 0))
    link = compare.get("html_url", "")
    if status == "identical":
        return Row(t, "current", False, "pin is the branch tip", link)
    if status == "ahead":
        return Row(t, f"{ahead} new", False, f"{ahead} commit(s) on `{t.branch}` since the pin", link)
    if status == "diverged":
        return Row(t, "diverged", True,
                   f"`{t.branch}` does not contain the pin ({ahead} ahead, {behind} behind): "
                   "the branch was rewritten, or track_ref names the wrong branch", link)
    if status == "behind":
        return Row(t, "branch behind", True,
                   f"`{t.branch}` is {behind} commit(s) behind the pin: the branch was reset", link)
    return Row(t, "unknown", True, f"unexpected compare status {status!r}", link)


def _get(url: str) -> tuple[int, dict | None]:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        return e.code, None


def probe(t: Target) -> Row:
    owner, repo = t.owner_repo
    api = f"https://api.github.com/repos/{owner}/{repo}"
    code, compare = _get(f"{api}/compare/{t.pin}...{t.branch}")
    if code == 200 and compare is not None:
        return classify(t, compare)
    pin_code, _ = _get(f"{api}/commits/{t.pin}")
    br_code, _ = _get(f"{api}/branches/{t.branch}")
    return classify(t, None, pin_code, br_code)


def render(rows: list[Row]) -> str:
    alerts = [r for r in rows if r.alert]
    lines = [
        "Weekly check of every pinned runner source against the branch it tracks. "
        "Nothing is built from this report; a bump follows docs/BUMPING.md.",
        "",
        f"**{len(alerts)} alert(s).**" if alerts else "**No alerts.**",
        "",
        "| target | repo | pin | tracks | state | detail |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        owner, repo = r.target.owner_repo
        state = f"⚠️ {r.state}" if r.alert else r.state
        detail = f"[{r.detail}]({r.link})" if r.link else r.detail
        lines.append(f"| `{r.target.name}` | {owner}/{repo} | `{r.target.pin[:7]}` | "
                     f"`{r.target.branch}` | {state} | {detail} |")
    retire = [r for r in rows if r.target.retire_when]
    if retire:
        lines += ["", "Retire conditions to re-check when you review a bump:", ""]
        lines += [f"- `{r.target.name}`: {r.target.retire_when}" for r in retire]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    targets = load_targets()
    if "--offline" in argv:
        for t in targets:
            print(f"{t.name}\t{t.repo_url}\t{t.pin}\t{t.track_ref}")
        return 0
    rows = [probe(t) for t in targets]
    sys.stdout.write(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
