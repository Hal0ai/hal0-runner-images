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
        # A failed pinned-commit probe is an alert, never "current" (Codex P2 on #15).
        (GH, "b100", 200, 500, "could not check the pinned commit (HTTP 500)", True),
        (GH, "b100", 200, 403, "could not check the pinned commit (HTTP 403)", True),
        (GH, "b100", 200, sw.NO_ANSWER, "could not check the pinned commit (no answer", True),
        (GH, None, sw.NO_ANSWER, 200, "upstream did not answer (no answer", True),
    ]
    for t, latest, api, pin, state, alert in cases:
        r = sw.classify(t, latest, api, pin)
        check(r.state.startswith(state) and r.alert == alert,
              f"classify({t.key}, {latest}, {api}, {pin}) -> {r.state!r}/{r.alert}, want {state!r}/{alert}")


def test_targets() -> None:
    targets = {t.key: t for t in sw.load_targets()}
    for want in ("llama.cpp.toml", "flm", "kokoro", "moonshine", "comfyui", "qwen3tts"):
        check(want in targets, f"load_targets() missing {want}")
    for followed in ("standard", "cpu"):
        check(followed not in targets, f"{followed} follows runners/llama.cpp.toml and must not be listed on its own")
    pin = targets.get("llama.cpp.toml")
    check(pin is not None and pin.followers == ["cpu", "standard", "cuda"] and pin.prereleases,
          f"llama.cpp.toml row must list its followers and count prereleases, got {pin}")
    for t in targets.values():
        if t.kind == "github-release":
            t.owner_repo  # raises SystemExit on a non-GitHub URL
            check(bool(t.pattern), f"{t.key}: github-release needs a pattern")
        else:
            check(bool(t.package), f"{t.key}: pypi needs a package")


def test_render() -> None:
    shared = sw.Target("llama.cpp.toml", "github-release", "https://github.com/acme/llama.cpp.git", "b100",
                       pattern=r"^b[0-9]+$", followers=["cpu", "standard", "cuda"])
    rows = [sw.classify(GH, "b101"), sw.classify(PY, None, 503), sw.classify(shared, "b100")]
    out = sw.render(rows)
    check("`llama.cpp.toml` (cpu, standard, cuda)" in out, "render: shared pin row must name its followers")
    check("**1 alert(s).**" in out, "render: alert count missing")
    check("1 runner(s) have a newer release" in out, "render: update count missing")
    out2 = sw.render([sw.classify(shared, "b101")])
    check("bumped via llama.cpp.toml" in out2, "render: shared pin update must say the release watch bumps it")
    check("⚠️ upstream did not answer" in out, "render: alert row not marked")


def test_transport_errors() -> None:
    """DNS/connection/timeout failures become alert rows; nothing raises (Codex P2 on #15)."""
    import socket
    import urllib.error
    import urllib.request

    real = urllib.request.urlopen
    for exc in (urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
                ConnectionRefusedError(111, "Connection refused"),
                TimeoutError("timed out")):
        def boom(*_a, _exc=exc, **_k):
            raise _exc
        urllib.request.urlopen = boom
        try:
            rows = [sw.probe(GH), sw.probe(PY)]
        except Exception as e:  # noqa: BLE001 - the point is that nothing escapes
            failures.append(f"probe raised {e!r} on {exc!r}")
            continue
        finally:
            urllib.request.urlopen = real
        for r in rows:
            check(r.alert and "no answer" in r.state, f"{exc!r}: {r.target.key} -> {r.state!r}/{r.alert}")
        out = sw.render(rows)
        check("**2 alert(s).**" in out, f"{exc!r}: render must still produce the report")


def test_truncated_bodies() -> None:
    """A response cut off mid-body becomes an alert row, not a crash (CodeRabbit on #17)."""
    import http.client
    import io
    import urllib.request

    class Resp(io.BytesIO):
        status = 200

        def __init__(self, data: bytes = b"", exc: Exception | None = None):
            super().__init__(data)
            self._exc = exc

        def read(self, *a):
            if self._exc is not None:
                raise self._exc
            return super().read(*a)

    cases = {
        "IncompleteRead": lambda: Resp(exc=http.client.IncompleteRead(b'{"info": {"vers', 40)),
        "partial JSON": lambda: Resp(b'{"info": {"version": "0.4'),
    }
    real = urllib.request.urlopen
    for name, make in list(cases.items()) + [("RemoteDisconnected", None)]:
        def fake(*_a, _make=make, **_k):
            if _make is None:
                raise http.client.RemoteDisconnected("Remote end closed connection without response")
            return _make()
        urllib.request.urlopen = fake
        try:
            rows = [sw.probe(GH), sw.probe(PY)]
        except Exception as e:  # noqa: BLE001 - the point is that nothing escapes
            failures.append(f"{name}: probe raised {e!r}")
            continue
        finally:
            urllib.request.urlopen = real
        for r in rows:
            check(r.alert and "no answer" in r.state, f"{name}: {r.target.key} -> {r.state!r}/{r.alert}")


def main() -> int:
    for t in (test_newest_release, test_classify, test_targets, test_render, test_transport_errors,
              test_truncated_bodies):
        before = len(failures)
        t()
        print(f"{'FAIL' if len(failures) > before else 'ok  '} {t.__name__}")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
