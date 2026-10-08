#!/usr/bin/env python3
"""Fixture checks for scripts/fork_watch.py. No network.

    python3 scripts/test_fork_watch.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fork_watch as fw  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


T = fw.Target("runners/x", "https://github.com/acme/llama.cpp.git", "a" * 40, "refs/heads/main", "when y")


def test_classify() -> None:
    cases = [
        ({"status": "identical"}, None, None, "current", False),
        ({"status": "ahead", "ahead_by": 12}, None, None, "12 new", False),
        ({"status": "diverged", "ahead_by": 3, "behind_by": 5}, None, None, "diverged", True),
        ({"status": "behind", "behind_by": 2}, None, None, "branch behind", True),
        (None, 404, 200, "pin gone", True),
        (None, 422, 200, "pin gone", True),
        (None, 200, 404, "branch gone", True),
        # A rate limit or proxy refusal is never a verdict about the fork.
        (None, 403, 403, "unknown", True),
        (None, 502, 200, "unknown", True),
    ]
    for compare, pin, branch, state, alert in cases:
        r = fw.classify(T, compare, pin, branch)
        check(r.state == state and r.alert == alert,
              f"classify({compare}, {pin}, {branch}) -> {r.state}/{r.alert}, want {state}/{alert}")


def test_targets() -> None:
    names = [t.name for t in fw.load_targets()]
    for want in ("runners/rocmfpx", "runners/upstream", "runners/strix", "runners/promptforge",
                 "strix-base", "llama-vulkan"):
        check(want in names, f"load_targets() missing {want}")
    for t in fw.load_targets():
        t.owner_repo  # raises SystemExit on a non-GitHub URL
        check(len(t.pin) == 40, f"{t.name}: pin is not a full SHA")


def test_render() -> None:
    rows = [fw.classify(T, {"status": "ahead", "ahead_by": 1, "html_url": "https://x"}),
            fw.classify(T, None, 404, 200)]
    out = fw.render(rows)
    check("**1 alert(s).**" in out, "render: alert count missing")
    check("⚠️ pin gone" in out, "render: alert row not marked")
    check("when y" in out, "render: retire conditions missing")


def main() -> int:
    for t in (test_classify, test_targets, test_render):
        before = len(failures)
        t()
        print(f"{'FAIL' if len(failures) > before else 'ok  '} {t.__name__}")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
