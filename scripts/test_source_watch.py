#!/usr/bin/env python3
"""Fixture checks for scripts/source_watch.py. No network.

    python3 scripts/test_source_watch.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_watch as sw  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


GH = sw.Target("x", "github-release", "https://github.com/acme/llama.cpp.git", "b100",
               commit="a" * 40, pattern=r"^b[0-9]+$")
PY = sw.Target("y", "pypi", "https://github.com/acme/tts", "0.4.9", package="acme-tts")


def test_newest_release() -> None:
    releases = [
        {"tag_name": "v0.6.0", "published_at": "2026-10-12T00:00:00Z"},  # other release line
        {"tag_name": "b101", "published_at": "2026-10-10T00:00:00Z"},
        {"tag_name": "b102", "published_at": "2026-10-11T00:00:00Z", "prerelease": True},
        {"tag_name": "b103", "published_at": "2026-10-11T01:00:00Z", "draft": True},
        {"tag_name": "b100", "published_at": "2026-10-09T00:00:00Z"},
    ]
    got = sw.newest_release(releases, r"^b[0-9]+$")
    check(got == "b101", f"newest_release -> {got}, want b101 (semver line, drafts, prereleases ignored)")
    check(sw.newest_release([], r"^b[0-9]+$") is None, "newest_release of nothing must be None")
    # ggml-org marks every bNNNNN build release as a prerelease.
    got = sw.newest_release(releases, r"^b[0-9]+$", prereleases=True)
    check(got == "b102", f"newest_release(prereleases=True) -> {got}, want b102")


def test_classify() -> None:
    cases = [
        (GH, "b100", 200, 200, "current", False),
        (GH, "b101", 200, 200, "newer release available", False),
        (GH, "b101", 200, 404, "pinned commit gone", True),
        (GH, None, 200, 200, "no release found", True),
        # A rate limit or proxy refusal is never a verdict about upstream.
        (GH, None, 403, 200, "upstream did not answer", True),
        (PY, "0.4.9", 200, 200, "current", False),
        (sw.Target("z", "github-release", "https://github.com/acme/ui", "0.28.0", pattern="^v"), "v0.28.0",
         200, 200, "current", False),
        (PY, "0.5.0", 200, 200, "newer release available", False),
        (PY, None, 404, 200, "upstream did not answer", True),
    ]
    for t, latest, api, pin, state, alert in cases:
        r = sw.classify(t, latest, api, pin)
        check(r.state.startswith(state) and r.alert == alert,
              f"classify({t.key}, {latest}, {api}, {pin}) -> {r.state!r}/{r.alert}, want {state!r}/{alert}")


def test_targets() -> None:
    targets = {t.key: t for t in sw.load_targets()}
    for want in ("standard", "cpu", "flm", "kokoro", "moonshine", "comfyui", "qwen3tts"):
        check(want in targets, f"load_targets() missing {want}")
    for t in targets.values():
        if t.kind == "github-release":
            t.owner_repo  # raises SystemExit on a non-GitHub URL
            check(bool(t.pattern), f"{t.key}: github-release needs a pattern")
        else:
            check(bool(t.package), f"{t.key}: pypi needs a package")


def test_render() -> None:
    rows = [sw.classify(GH, "b101"), sw.classify(PY, None, 503)]
    out = sw.render(rows)
    check("**1 alert(s).**" in out, "render: alert count missing")
    check("1 runner(s) have a newer release" in out, "render: update count missing")
    check("⚠️ upstream did not answer" in out, "render: alert row not marked")


def main() -> int:
    for t in (test_newest_release, test_classify, test_targets, test_render):
        before = len(failures)
        t()
        print(f"{'FAIL' if len(failures) > before else 'ok  '} {t.__name__}")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
