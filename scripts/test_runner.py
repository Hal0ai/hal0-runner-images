#!/usr/bin/env python3
"""Checks for scripts/runner.py's tag inputs. No network, no container runtime.

    python3 scripts/test_runner.py

Runs runner.py's tag logic against a throwaway git repository, so it needs
only git.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import runner  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def git(root: Path, *argv: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *argv],
                   cwd=root, check=True, capture_output=True)


def test_build_driver_is_a_tag_input() -> None:
    for key in runner.keys():
        paths = runner.recipe_paths(key)
        for driver in runner.BUILD_DRIVER:
            check(driver in paths, f"runners/{key}: recipe_paths must include the build driver {driver}")
            check((runner.ROOT / driver).is_file(), f"BUILD_DRIVER names a missing file {driver}")


def test_tag_moves_with_each_input() -> None:
    """Changing the recipe, the shared pin or the build driver each yields a new tag (Codex P2 on #15)."""
    tmp = Path(tempfile.mkdtemp(prefix="hal0-runner-tag-"))
    old_root, old_runners = runner.ROOT, runner.RUNNERS
    try:
        (tmp / "scripts").mkdir()
        shutil.copy2(HERE / "runner.py", tmp / "scripts" / "runner.py")
        (tmp / "runners" / "demo").mkdir(parents=True)
        (tmp / "runners" / "other").mkdir(parents=True)
        (tmp / "runners" / "llama.cpp.toml").write_text("[llama_cpp]\nversion = 'b1'\n")
        (tmp / "runners" / "demo" / "manifest.toml").write_text("[source]\npin = 'llama.cpp.toml'\n")
        (tmp / "runners" / "other" / "manifest.toml").write_text("[source]\n")
        git(tmp, "init", "-q")
        git(tmp, "add", "-A")
        git(tmp, "commit", "-q", "-m", "init")
        runner.ROOT, runner.RUNNERS = tmp, tmp / "runners"
        doc = {"image": {"version": "b1"}}

        def tag() -> str:
            return runner.tag_for("demo", doc)

        def commit(path: str, text: str) -> None:
            (tmp / path).write_text(text)
            git(tmp, "commit", "-q", "-am", path)

        first = tag()
        commit("runners/other/manifest.toml", "[source]\n# unrelated\n")
        check(tag() == first, "a change to another runner must not move this runner's tag")
        for path, text in (("scripts/runner.py", (HERE / "runner.py").read_text() + "\n# build logic changed\n"),
                           ("runners/llama.cpp.toml", "[llama_cpp]\nversion = 'b1'\n# moved\n"),
                           ("runners/demo/manifest.toml", "[source]\npin = 'llama.cpp.toml'\n# edited\n")):
            before = tag()
            commit(path, text)
            check(tag() != before, f"changing {path} must give a new tag (still {before})")
    finally:
        runner.ROOT, runner.RUNNERS = old_root, old_runners
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    for t in (test_build_driver_is_a_tag_input, test_tag_moves_with_each_input):
        before = len(failures)
        t()
        print(f"{'FAIL' if len(failures) > before else 'ok  '} {t.__name__}")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
