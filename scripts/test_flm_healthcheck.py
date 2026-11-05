#!/usr/bin/env python3
"""Tests for runners/flm/healthcheck.sh's port resolution.

    python3 scripts/test_flm_healthcheck.py

No network beyond a loopback http.server; builds a fake /proc tree so the
script's `flm serve --port N` discovery can be exercised without a real
flm process.
"""

from __future__ import annotations

import http.server
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "runners" / "flm" / "healthcheck.sh"

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: D401,A002 - silence access logs
        pass


class FakeServer:
    """A loopback http.server serving v1/models from a temp dir."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="hal0-flm-hc-"))
        (self.dir / "v1").mkdir()
        (self.dir / "v1" / "models").write_text("{}")
        self.port = free_port()
        handler = lambda *a, **kw: QuietHandler(*a, directory=str(self.dir), **kw)  # noqa: E731
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.dir, ignore_errors=True)


def write_cmdline(proc_root: Path, pid: str, argv: list[str]) -> None:
    pdir = proc_root / pid
    pdir.mkdir(parents=True)
    (pdir / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")


def run_healthcheck(env_extra: dict[str, str]) -> int:
    env = dict(os.environ)
    env.pop("HAL0_PORT", None)
    env.update(env_extra)
    proc = subprocess.run(["/bin/sh", str(SCRIPT)], env=env, capture_output=True, text=True)
    return proc.returncode


def test_hal0_port_env_wins() -> None:
    srv = FakeServer()
    tmp = Path(tempfile.mkdtemp(prefix="hal0-flm-proc-"))
    try:
        rc = run_healthcheck({"HAL0_PORT": str(srv.port), "HAL0_FLM_PROC": str(tmp)})
        check(rc == 0, f"(a) HAL0_PORT set to a live port must succeed, got rc={rc}")
    finally:
        srv.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_discovers_port_flag_form() -> None:
    srv = FakeServer()
    tmp = Path(tempfile.mkdtemp(prefix="hal0-flm-proc-"))
    try:
        write_cmdline(tmp, "100", ["/usr/local/bin/flm", "serve", "mytag", "--port", str(srv.port)])
        rc = run_healthcheck({"HAL0_FLM_PROC": str(tmp)})
        check(rc == 0, f"(b) --port <N> form on a live port must succeed, got rc={rc}")
    finally:
        srv.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_discovers_port_equals_form() -> None:
    srv = FakeServer()
    tmp = Path(tempfile.mkdtemp(prefix="hal0-flm-proc-"))
    try:
        write_cmdline(tmp, "101", ["/usr/local/bin/flm", "serve", "mytag", f"--port={srv.port}"])
        rc = run_healthcheck({"HAL0_FLM_PROC": str(tmp)})
        check(rc == 0, f"(c) --port=<N> form on a live port must succeed, got rc={rc}")
    finally:
        srv.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_dead_port_fails() -> None:
    dead = free_port()  # free == nothing listening
    tmp = Path(tempfile.mkdtemp(prefix="hal0-flm-proc-"))
    try:
        write_cmdline(tmp, "102", ["/usr/local/bin/flm", "serve", "mytag", "--port", str(dead)])
        rc = run_healthcheck({"HAL0_FLM_PROC": str(tmp)})
        check(rc != 0, f"(d) a dead discovered port must fail, got rc={rc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_flm_process_falls_back_to_8086() -> None:
    if not port_is_free(8086):
        return  # skip: something is already listening on 8086 on this host
    tmp = Path(tempfile.mkdtemp(prefix="hal0-flm-proc-"))
    try:
        rc = run_healthcheck({"HAL0_FLM_PROC": str(tmp)})
        check(rc != 0, f"(e) no flm process and nothing on 8086 must fail, got rc={rc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_non_flm_process_with_port_flag_is_ignored() -> None:
    srv = FakeServer()
    tmp = Path(tempfile.mkdtemp(prefix="hal0-flm-proc-"))
    try:
        write_cmdline(tmp, "103", ["/usr/local/bin/not-flm", "serve", "mytag", "--port", str(srv.port)])
        rc = run_healthcheck({"HAL0_FLM_PROC": str(tmp)})
        check(rc != 0, f"(f) a non-flm process must be ignored (fall back, non-zero), got rc={rc}")
    finally:
        srv.close()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    check(SCRIPT.is_file(), f"{SCRIPT} must exist")
    if SCRIPT.is_file():
        test_hal0_port_env_wins()
        test_discovers_port_flag_form()
        test_discovers_port_equals_form()
        test_dead_port_fails()
        test_no_flm_process_falls_back_to_8086()
        test_non_flm_process_with_port_flag_is_ignored()

    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f" - {f}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
