#!/usr/bin/env python3
"""Hardware gate for a published runner image (docs/UPDATING.md section 4).

Run it on the Strix Halo box, never in CI. It pulls `image@digest`, runs the
probes of `gates/<key>.toml` against it and writes a `hal0.gate-report.v1`
document that `ci/promote.py` accepts. The probes live outside runners/<key>/
so that changing one never changes an image tag; the manifest is read only for
the image's identity (package, version, source ref):

    python3 scripts/gate.py standard --digest sha256:<64 hex> [--tag b11552-r1a2b3c]
            [--models gate-models.toml] [--models-dir DIR] [--out report.json]
            [--allow-skip PROBE ...] [--headroom-gib 8] [--upload]
    python3 scripts/gate.py --group llama.cpp \\
            --digest standard=sha256:<64 hex> --digest cpu=sha256:<64 hex>
    python3 scripts/gate.py standard --plan     # print the containers it would run; runs nothing

stdlib only. What it does, in order, per runner:

  1. pulls image@digest, checks its labels (runner key, immutable tag) and that
     the registry tag still resolves to that digest;
  2. for every lane in gates/<key>.toml (rocm, vulkan, cpu, npu), runs its
     probes and sessions, ONE container and ONE model at a time.
     Containers are named gate-<key>-*, publish on 127.0.0.1 only, mount models
     read-only, and get the device and security flags of a hal0 slot;
  3. before every model load, reads host memory (/sys/devices/system/node/node0
     /meminfo, and /proc/meminfo; the lower wins) and refuses a load that would
     leave less than --headroom-gib available. Such a probe is reported as
     skipped, which fails the gate unless --allow-skip names it;
  4. removes every container it started, on success, failure or Ctrl-C.

It never touches hal0's slots, units or configuration. Model files come from
gate-models.toml (see docs/UPDATING.md), kept outside this repository; the
report names models by key and file name only, and host names, addresses and
operator paths are scrubbed from it.

Exit status: 0 passed, 1 the gate failed, 2 the gate could not run.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import dataclasses
import datetime as dt
import fcntl
import fnmatch
import glob
import grp
import http.client
import json
import math
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from array import array
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
GATES = ROOT / "gates"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "ci"))
import runner  # noqa: E402
import promote  # noqa: E402  (promote's own report validator; never a copy of it)

SCHEMA = promote.GATE_SCHEMA
REPO_SLUG = "Hal0ai/hal0-runner-images"
GIB = 1 << 30
DEFAULT_HEADROOM_GIB = 8.0
MODELS_ENV = "HAL0_GATE_MODELS"
DEFAULT_MODELS_FILE = "~/.config/hal0/gate-models.toml"
IN_CONTAINER_MODELS = "/gate/models"
GARBAGE_TARGET = "/gate/garbage.gguf"
DETAIL_MAX = 600
KEY_LABEL = "dev.hal0.runner.key"
TAG_LABEL = promote.TAG_LABEL
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


class GateError(SystemExit):
    """The gate could not run (bad input, no image, no podman). Exit 2."""

    def __init__(self, msg: str) -> None:
        super().__init__(2)
        self.msg = f"error: {msg}"

    def __str__(self) -> str:
        return self.msg


class Skip(Exception):
    """A probe that could not run: no model configured, no device, no memory."""


class Fail(Exception):
    """A probe that ran and failed."""


# ---------------------------------------------------------------------------
# The plan: gates/<key>.toml (pure, tested)
# ---------------------------------------------------------------------------

LANE_FIELDS = {"devices", "server_args", "podman_args"}
PROBE_FIELDS = {
    "exec": {"args", "entrypoint", "expect", "reject", "exit", "lane_args", "no_devices", "timeout",
             "mem_gib"},
    "perplexity": {"model", "text", "compare", "ctx", "chunks", "max_diff_pct", "entrypoint", "timeout"},
    "elsewhere": set(),
}
SESSION_FIELDS = {"name", "doc", "lanes", "args", "entrypoint", "port", "health", "ready", "startup_timeout",
                  "mem_extra_gib", "mem_gib", "mounts", "cache", "env", "informational", "check"}
#: check kind -> (required params, optional params)
CHECK_KINDS: dict[str, tuple[set, set]] = {
    "http-json": (set(), {"path", "equals", "contains", "nonempty", "no_port_arg"}),
    "completion": ({"prompt", "expect"}, {"n_predict"}),
    "chat": ({"prompt", "expect"}, {"model", "max_tokens", "thinking"}),
    "structured": (set(), {"formats", "thinking", "max_tokens"}),
    "grammar-size": (set(), {"min_length", "objects"}),
    "tool-call": (set(), {"max_tokens"}),
    "large-tools": (set(), {"min_prompt_tokens", "max_tokens"}),
    "concurrency": (set(), {"parallel", "require_identical"}),
    "mtp": ({"role"}, {"prompt", "n_predict"}),
    "resident-memory": (set(), set()),
    "log": (set(), {"expect", "reject", "at_least"}),
    "custom-nodes": ({"names"}, set()),
    "speech": ({"input"}, {"voice", "language", "min_seconds"}),
    "transcribe": ({"sample"}, {"min_recall"}),
    "stream": ({"sample"}, {"timeout"}),
    "comfy-generate": ({"checkpoint"}, {"width", "height", "steps", "timeout"}),
}
CHECK_COMMON = {"name", "doc", "kind", "informational", "timeout"}

#: {path:KEY} host file mounted read-only; {value:KEY:FIELD} a field of a
#: gate-models.toml entry; {version} {version_digits} {ref7} from the manifest;
#: {garbage} a file that is not a model.
TOKEN = re.compile(r"\{(?:path:([^{}:]+)|value:([^{}:]+):(\w+)|(version|version_digits|ref7|garbage))\}")


def path_keys(texts: list[str]) -> list[str]:
    out: list[str] = []
    for t in texts:
        for m in TOKEN.finditer(t):
            if m.group(1) and m.group(1) not in out:
                out.append(m.group(1))
    return out


def render(text: str, resolve: Callable[[str, str, str], str], regex: bool = False) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1):
            v = resolve("path", m.group(1), "")
        elif m.group(2):
            v = resolve("value", m.group(2), m.group(3))
        else:
            v = resolve(m.group(4), "", "")
        return re.escape(v) if regex else v
    return TOKEN.sub(sub, text)


def _strs(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


GATE_KEYS = {"boxes", "lanes", "probe", "session"}


def validate_gate(ver: dict, backends: list[str]) -> list[str]:
    """Problems with a gates/<key>.toml document (the gate's input). Empty = fine."""
    out: list[str] = []
    if not isinstance(ver, dict):
        return ["not a TOML table"]
    for f in sorted(set(ver) - GATE_KEYS):
        out.append(f"unknown top-level key {f!r} (expected {sorted(GATE_KEYS)})")
    if not _strs(ver.get("boxes")) or not ver.get("boxes"):
        out.append("boxes must list the boxes the gate runs on")
    backends = set(backends)
    lanes = ver.get("lanes")
    if not isinstance(lanes, dict) or not lanes:
        out.append("[lanes] must define at least one lane")
        lanes = {}
    for lane, spec in lanes.items():
        if lane not in backends:
            out.append(f"lane {lane!r} is not one of [runner].backends {sorted(backends)}")
        if not isinstance(spec, dict):
            out.append(f"[lanes.{lane}] must be a table")
            continue
        for f in set(spec) - LANE_FIELDS:
            out.append(f"[lanes.{lane}]: unknown key {f!r}")
        for f in LANE_FIELDS & set(spec):
            if not _strs(spec[f]):
                out.append(f"[lanes.{lane}].{f} must be a list of strings")
        for d in spec.get("devices", []):
            if not str(d).startswith("/dev/"):
                out.append(f"[lanes.{lane}]: device {d!r} is not under /dev")
    names: list[str] = []

    def name_ok(where: str, item: dict) -> None:
        n = item.get("name")
        if not isinstance(n, str) or not NAME_RE.match(n):
            out.append(f"{where}: name {n!r} must be lowercase letters, digits, '.' and '-'")
        elif n in names:
            out.append(f"{where}: name {n!r} is used twice")
        else:
            names.append(n)
        if not isinstance(item.get("doc"), str) or not item["doc"].strip():
            out.append(f"{where} {n!r}: doc (what it proves) missing")

    def regexes_ok(where: str, pats: Any) -> None:
        if pats is None:
            return
        if not _strs(pats):
            out.append(f"{where}: must be a list of regular expressions")
            return
        for p in pats:
            try:
                re.compile(render(p, lambda *_: "x", regex=True))
            except re.error as e:
                out.append(f"{where}: {p!r} is not a regular expression ({e})")

    for i, p in enumerate(ver.get("probe", [])):
        where = f"[[probe]] #{i + 1}"
        if not isinstance(p, dict):
            out.append(f"{where} must be a table")
            continue
        name_ok(where, p)
        kind = p.get("kind")
        if kind not in PROBE_FIELDS:
            out.append(f"{where}: kind {kind!r} must be one of {sorted(PROBE_FIELDS)}")
            continue
        extra = set(p) - PROBE_FIELDS[kind] - {"name", "doc", "kind", "lanes"}
        if extra:
            out.append(f"{where}: unknown keys {sorted(extra)} for kind {kind!r}")
        if "lanes" in p and (not _strs(p["lanes"]) or not set(p["lanes"]) <= set(lanes)):
            out.append(f"{where}: lanes must be a subset of {sorted(lanes)}")
        if kind == "exec":
            if not _strs(p.get("args", [])):
                out.append(f"{where}: args must be a list of strings")
            regexes_ok(f"{where} expect", p.get("expect"))
            regexes_ok(f"{where} reject", p.get("reject"))
            if not isinstance(p.get("exit", 0), int):
                out.append(f"{where}: exit must be an integer")
        if kind == "perplexity":
            for f in ("model", "text"):
                if not isinstance(p.get(f), str):
                    out.append(f"{where}: {f} (a gate-models.toml key) missing")
            cmp = p.get("compare")
            if not _strs(cmp) or len(cmp) != 2 or not set(cmp) <= set(lanes):
                out.append(f"{where}: compare must name two lanes of {sorted(lanes)}")
    sessions = ver.get("session", [])
    for i, s in enumerate(sessions):
        where = f"[[session]] #{i + 1}"
        if not isinstance(s, dict):
            out.append(f"{where} must be a table")
            continue
        name_ok(where, s)
        for f in set(s) - SESSION_FIELDS:
            out.append(f"{where}: unknown key {f!r}")
        if not _strs(s.get("args", [])):
            out.append(f"{where}: args must be a list of strings")
        if not isinstance(s.get("port"), int):
            out.append(f"{where}: port (the server's port in the container) must be an integer")
        if "lanes" in s and (not _strs(s["lanes"]) or not set(s["lanes"]) <= set(lanes)):
            out.append(f"{where}: lanes must be a subset of {sorted(lanes)}")
        for m in s.get("mounts", []) + s.get("cache", []):
            if not isinstance(m, dict) or not isinstance(m.get("key"), str) or not str(m.get("target", "")).startswith("/"):
                out.append(f"{where}: mounts and cache entries are {{ key = ..., target = \"/abs\" }}")
        checks = s.get("check", [])
        if not checks:
            out.append(f"{where}: no [[session.check]]")
        for j, c in enumerate(checks):
            cw = f"{where} check #{j + 1}"
            if not isinstance(c, dict):
                out.append(f"{cw} must be a table")
                continue
            name_ok(cw, c)
            kind = c.get("kind")
            if kind not in CHECK_KINDS:
                out.append(f"{cw}: kind {kind!r} must be one of {sorted(CHECK_KINDS)}")
                continue
            need, opt = CHECK_KINDS[kind]
            missing = need - set(c)
            if missing:
                out.append(f"{cw}: {kind} needs {sorted(missing)}")
            extra = set(c) - need - opt - CHECK_COMMON
            if extra:
                out.append(f"{cw}: unknown keys {sorted(extra)} for kind {kind!r}")
            if kind == "log":
                regexes_ok(f"{cw} expect", c.get("expect"))
                regexes_ok(f"{cw} reject", c.get("reject"))
            if kind == "mtp" and c.get("role") not in ("on", "off"):
                out.append(f"{cw}: role must be 'on' or 'off'")
    if not ver.get("probe") and not sessions:
        out.append("no [[probe]] and no [[session]]")
    return out


def gate_file(key: str) -> Path:
    return GATES / f"{key}.toml"


def read_gate(key: str) -> dict:
    path = gate_file(key)
    if not path.is_file():
        raise GateError(f"runner {key!r} has no gates/{key}.toml")
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise GateError(f"gates/{key}.toml: {e}")


def load_plan(key: str) -> tuple[dict, dict]:
    """(the runner's manifest, for the image's identity; its gates/<key>.toml)."""
    doc = runner.load(key)
    ver = read_gate(key)
    problems = validate_gate(ver, doc["runner"]["backends"])
    if problems:
        raise GateError("gates/{}.toml:\n  {}".format(key, "\n  ".join(problems)))
    return doc, ver


def group_keys(group: str) -> list[str]:
    """Runners sharing one pin file: `--group llama.cpp` = every recipe with [source].pin = llama.cpp.toml."""
    keys = []
    for k in runner.keys():
        raw = tomllib.loads((runner.RUNNERS / k / "manifest.toml").read_text())
        if raw.get("source", {}).get("pin") == f"{group}.toml":
            keys.append(k)
    if not keys:
        raise GateError(f"no runner uses the shared pin runners/{group}.toml")
    return keys


# ---------------------------------------------------------------------------
# gate-models.toml (pure, tested)
# ---------------------------------------------------------------------------

class Models:
    """Model files the operator keeps outside the repository.

        root = "/srv/models"                     # optional; relative paths resolve here
        [models]
        "qwen3.8-27b" = "Qwen3.8-27B-UD-Q4_K_XL.gguf"
        [models.stt-sample]
        path = "/srv/gate/jfk-16k.wav"
        text = "ask not what your country can do for you"
    """

    def __init__(self, entries: dict[str, dict] | None = None, source: str = "") -> None:
        self.entries = entries or {}
        self.source = source

    @classmethod
    def parse(cls, text: str, base: str, models_dir: str | None = None, source: str = "") -> "Models":
        doc = tomllib.loads(text)
        extra = set(doc) - {"root", "models"}
        if extra:
            raise GateError(f"gate-models.toml: unknown top-level keys {sorted(extra)} (expected root, [models])")
        root = Path(os.path.expanduser(models_dir or doc.get("root") or base))
        entries: dict[str, dict] = {}
        for k, v in (doc.get("models") or {}).items():
            if isinstance(v, str):
                v = {"path": v}
            if not isinstance(v, dict) or not isinstance(v.get("path", ""), str):
                raise GateError(f"gate-models.toml: models.{k} must be a path or a table with path = ...")
            e = dict(v)
            if e.get("path"):
                p = Path(os.path.expanduser(e["path"]))
                e["path"] = str(p if p.is_absolute() else root / p)
            entries[k] = e
        return cls(entries, source)

    @classmethod
    def load(cls, path: str | None, models_dir: str | None) -> "Models":
        explicit = path or os.environ.get(MODELS_ENV)
        p = Path(os.path.expanduser(explicit or DEFAULT_MODELS_FILE))
        if not p.is_file():
            if explicit:
                raise GateError("the gate-models.toml given does not exist")
            return cls({}, "")
        return cls.parse(p.read_text(), str(p.parent), models_dir, str(p))

    def path(self, key: str) -> str:
        e = self.entries.get(key)
        if not e or not e.get("path"):
            raise Skip(f"model {key!r} is not configured in gate-models.toml")
        if not os.path.exists(e["path"]):
            raise Skip(f"the file for model {key!r} does not exist")
        return e["path"]

    def value(self, key: str, field: str) -> str:
        e = self.entries.get(key)
        if not e or field not in e:
            raise Skip(f"gate-models.toml has no {field!r} for {key!r}")
        return str(e[field])

    def host_paths(self) -> list[str]:
        out = [self.source] if self.source else []
        for e in self.entries.values():
            if e.get("path"):
                out += [e["path"], str(Path(e["path"]).parent)]
        return out


# ---------------------------------------------------------------------------
# Host memory (pure, tested)
# ---------------------------------------------------------------------------

NODE_MEMINFO = "/sys/devices/system/node/node0/meminfo"
#: One gate at a time per host (Host.lock). Not /tmp, $TMPDIR or
#: $XDG_RUNTIME_DIR: another user can pre-create or delete a file there, and
#: the gate runs as root. HAL0_GATE_LOCK overrides the path (same checks).
LOCK_FILE = "/run/lock/hal0-gate.lock"
LOCK_ENV = "HAL0_GATE_LOCK"


def lock_problem(st: os.stat_result, dir_st: os.stat_result, euid: int) -> str:
    """Why a lock file (st) in its directory (dir_st) cannot be trusted, or ''.

    Trusted: a regular, singly linked file owned by root or by us, writable by
    nobody else, in a directory owned by root or by us that others cannot write
    to, or that is sticky (so only the file's owner may remove or replace it).
    """
    import stat as st_
    owners = {0, euid}
    if not st_.S_ISDIR(dir_st.st_mode):
        return "its directory is not a directory"
    if dir_st.st_uid not in owners:
        return f"its directory is owned by uid {dir_st.st_uid}"
    if dir_st.st_mode & 0o022 and not dir_st.st_mode & st_.S_ISVTX:
        return "its directory is writable by others and not sticky"
    if not st_.S_ISREG(st.st_mode):
        return "it is not a regular file"
    if st.st_uid not in owners:
        return f"it is owned by uid {st.st_uid}, not root or this user"
    if st.st_mode & 0o022:
        return f"it is writable by others (mode {st.st_mode & 0o777:o})"
    if st.st_nlink != 1:
        return f"it has {st.st_nlink} hard links"
    return ""


def open_lock(path: str, euid: int | None = None) -> int:
    """Open (creating it 0644 if missing) and flock the host gate lock; return the fd.

    Never follows a symlink, never trusts a file someone else could replace,
    and checks after locking that the path still names the locked file.
    """
    euid = os.geteuid() if euid is None else euid
    flags = os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, os.O_RDONLY | flags)
    except FileNotFoundError:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | flags, 0o644)
            os.fchmod(fd, 0o644)
        except FileExistsError:
            fd = os.open(path, os.O_RDONLY | flags)
        except OSError as e:
            raise GateError(f"cannot create the host gate lock {path} ({e}); run the gate as root, "
                            f"or set {LOCK_ENV} to a file in a directory only the gate operators can write")
    except OSError as e:  # ELOOP: a symlink
        raise GateError(f"refusing the host gate lock {path}: {e}")
    try:
        problem = lock_problem(os.fstat(fd), os.stat(os.path.dirname(os.path.abspath(path))), euid)
        if problem:
            raise GateError(f"refusing the host gate lock {path}: {problem}. Remove it as root, "
                            f"or set {LOCK_ENV} to a protected path")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise GateError(f"another scripts/gate.py run holds {path}; one gate at a time on a host")
        now, mine = os.stat(path, follow_symlinks=False), os.fstat(fd)
        if (now.st_dev, now.st_ino) != (mine.st_dev, mine.st_ino):
            raise GateError(f"the host gate lock {path} was replaced while it was being taken")
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_new(path: Path, text: str) -> None:
    """Create a file that does not exist yet; never follow a symlink or overwrite
    (the gate runs as root and the report name is predictable)."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    except FileExistsError:
        raise GateError(f"{path} already exists; choose another --out")
    with os.fdopen(fd, "w") as f:
        f.write(text)


def parse_meminfo(text: str) -> dict[str, int]:
    """Bytes per field of /proc/meminfo or a NUMA node's meminfo ("Node 0 MemFree: ... kB")."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(?:Node \d+\s+)?([A-Za-z0-9_()]+):\s+(\d+)\s*(kB)?\s*$", line)
        if m:
            out[m.group(1)] = int(m.group(2)) * (1024 if m.group(3) else 1)
    return out


def node_available(info: dict[str, int]) -> int:
    """MemAvailable for a NUMA node, which the kernel does not report per node:
    free pages plus the file LRU and reclaimable slab (shmem is not reclaimable
    and lives on the anon LRU, so it is left out)."""
    return sum(info.get(k, 0) for k in ("MemFree", "Active(file)", "Inactive(file)", "SReclaimable"))


def available_memory(node_text: str | None, proc_text: str | None) -> tuple[int, str]:
    """(bytes available, source). The lower of node0 (the host, also inside an
    LXC container) and /proc/meminfo (a container's memory limit) wins."""
    cands: list[tuple[int, str]] = []
    if node_text:
        info = parse_meminfo(node_text)
        if "MemFree" in info:
            cands.append((node_available(info), "node0"))
    if proc_text:
        info = parse_meminfo(proc_text)
        if "MemAvailable" in info:
            cands.append((info["MemAvailable"], "meminfo"))
    if not cands:
        raise GateError("cannot read host memory (node0 meminfo and /proc/meminfo both unreadable)")
    return min(cands)


def gib(n: float) -> str:
    return f"{n / GIB:.1f} GiB"


def headroom_decision(available: int, need: int, headroom: int) -> tuple[bool, str]:
    left = available - need
    detail = (f"{gib(available)} available, the load needs about {gib(need)}, "
              f"leaving {gib(left)} (floor {gib(headroom)})")
    return left >= headroom, detail


# ---------------------------------------------------------------------------
# Containers (pure argv builders, tested)
# ---------------------------------------------------------------------------

SECURITY = ["--security-opt", "label=disable", "--security-opt", "seccomp=unconfined",
            "--security-opt", "apparmor=unconfined"]


def podman_run_argv(*, podman: str, name: str, image: str, args: list[str], entrypoint: str | None = None,
                    devices: list[str] = (), groups: list[str] = (), podman_args: list[str] = (),
                    mounts: list[tuple[str, str, bool]] = (), env: dict[str, str] | None = None,
                    publish: tuple[int, int] | None = None, detach: bool = False) -> list[str]:
    """`podman run` for a gate container: hal0 slot device and security flags,
    models read-only, published on 127.0.0.1 only, never pulled implicitly."""
    argv = [podman, "run", "--name", name, "--pull=never", *SECURITY]
    if detach:
        argv.append("-d")
    for d in devices:
        argv += ["--device", d]
    argv += list(groups) + list(podman_args)
    for host, target, ro in mounts:
        if ":" in host or ":" in target:
            raise Skip("a model path contains ':', which podman -v cannot mount")
        argv += ["-v", f"{host}:{target}" + (":ro" if ro else "")]
    for k, v in sorted((env or {}).items()):
        argv += ["-e", f"{k}={v}"]
    if publish:
        argv += ["-p", f"127.0.0.1:{publish[0]}:{publish[1]}"]
    if entrypoint:
        argv += ["--entrypoint", entrypoint]
    return argv + [image, *args]


def group_args(euid: int, gids: dict[str, int]) -> list[str]:
    """Rootless podman keeps the caller's supplementary groups (render, video);
    rootful podman gets the host's numeric render and video gids, as hal0's
    slots do (an image's /etc/group need not know them)."""
    if euid != 0:
        return ["--group-add", "keep-groups"]
    out: list[str] = []
    for g in ("video", "render"):
        if g in gids:
            out += ["--group-add", str(gids[g])]
    return out


def expand_devices(devices: list[str], globber: Callable[[str], list[str]] = glob.glob,
                   exists: Callable[[str], bool] = os.path.exists,
                   isdir: Callable[[str], bool] = os.path.isdir) -> tuple[list[str], list[str]]:
    """(device nodes to pass, missing). podman does not recurse a directory
    like /dev/dri, so it expands to its card and render nodes."""
    present, missing = [], []
    for d in devices:
        if isdir(d):
            nodes = sorted(globber(f"{d}/card*") + globber(f"{d}/renderD*"))
            (present.extend(nodes) if nodes else missing.append(d))
        elif exists(d):
            present.append(d)
        else:
            missing.append(d)
    return present, missing


def default_target(key: str, host_path: str, is_dir: bool) -> str:
    return f"{IN_CONTAINER_MODELS}/{key}" + ("" if is_dir else f"/{Path(host_path).name}")


# ---------------------------------------------------------------------------
# Parsers and payloads for the checks (pure, tested)
# ---------------------------------------------------------------------------

def clip(text: Any, n: int = 160) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def wav_stats(raw: bytes) -> tuple[float, float]:
    """(seconds, RMS in [0, 1]) of a RIFF/WAVE body: PCM 16/32-bit or float32."""
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise Fail("the response is not a WAV file")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid, size = raw[pos:pos + 4], struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        body = raw[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            data = body
            break
        pos += 8 + size + (size & 1)
    if not fmt or data is None:
        raise Fail("the WAV has no fmt or data chunk")
    tag, channels, rate, _, _, bits = fmt
    if tag == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE: the sub-format decides
        tag = 3 if bits == 32 and b"\x03\x00\x00\x00\x00\x00\x10\x00" in raw[:200] else 1
    width = bits // 8
    if not channels or not rate or not width:
        raise Fail("the WAV header is malformed")
    data = data[: len(data) - len(data) % width]
    if tag == 3 and width == 4:
        samples = array("f", data)
        scale = 1.0
    elif tag == 1 and width in (2, 4):
        samples = array("h" if width == 2 else "i", data)
        scale = float(1 << (bits - 1))
    else:
        raise Fail(f"unsupported WAV encoding (format {tag}, {bits} bit)")
    if sys.byteorder != "little":
        samples.byteswap()
    n = len(samples)
    seconds = n / channels / rate
    rms = math.sqrt(sum((s / scale) ** 2 for s in samples) / n) if n else 0.0
    return seconds, rms


def pcm16_mono_16k(raw: bytes) -> bytes:
    """The PCM payload of a 16 kHz mono 16-bit WAV (what the stream endpoint takes)."""
    pos = 12
    fmt = None
    while pos + 8 <= len(raw):
        cid, size = raw[pos:pos + 4], struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", raw[pos + 8:pos + 24])
        elif cid == b"data":
            if not fmt or fmt[0] != 1 or fmt[1] != 1 or fmt[2] != 16000 or fmt[5] != 16:
                raise Skip("the stream probe needs a 16 kHz mono 16-bit PCM WAV sample")
            return raw[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    raise Skip("the sample is not a WAV file")


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def word_recall(expected: str, got: str) -> float:
    want = words(expected)
    have = set(words(got))
    return sum(w in have for w in want) / len(want) if want else 0.0


def multipart(fields: dict[str, str], file: tuple[str, str, bytes, str], boundary: str) -> bytes:
    out = b""
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    field, filename, data, ctype = file
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"
    return out + f"--{boundary}--\r\n".encode()


def parse_ppl(text: str) -> float | None:
    m = re.findall(r"Final estimate:\s*PPL\s*=\s*([0-9.]+)", text)
    return float(m[-1]) if m else None


def draft_stats(timings: dict, logs: str) -> tuple[int | None, int | None]:
    """(drafted, accepted) tokens: llama-server's response timings, else its log line."""
    n, a = timings.get("draft_n"), timings.get("draft_n_accepted")
    if isinstance(n, int) and isinstance(a, int):
        return n, a
    m = re.findall(r"draft acceptance rate = [0-9.]+ \(\s*(\d+) accepted /\s*(\d+) generated\)", logs)
    if m:
        return int(m[-1][1]), int(m[-1][0])
    return None, None


def custom_node_status(logs: str) -> dict[str, bool]:
    """ComfyUI's "Import times for custom nodes" lines: name -> imported.

    ComfyUI 0.28 prefixes every log line with a coloured level, `\\x1b[32m[INFO]\\x1b[0m`,
    even when stdout is not a terminal; the escapes are stripped and the level skipped."""
    out: dict[str, bool] = {}
    plain = re.sub(r"\x1b\[[0-9;]*m", "", logs)
    for m in re.finditer(r"^(?:\[[A-Z]+\])?\s*[0-9.]+ seconds( \(IMPORT FAILED\))?: (.+?)\s*$", plain, re.M):
        out[Path(m.group(2)).name] = not m.group(1)
    return out


def png_size(raw: bytes) -> tuple[int, int]:
    if raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        raise Fail("the output is not a PNG")
    return struct.unpack(">II", raw[16:24])


def message_of(data: Any) -> dict:
    try:
        return data["choices"][0]["message"] or {}
    except (KeyError, IndexError, TypeError):
        return {}


def first_tool_call(data: Any) -> tuple[str, dict | None, str]:
    """(function name, parsed arguments or None, raw arguments) of the first tool call."""
    calls = message_of(data).get("tool_calls") or []
    if not calls:
        return "", None, ""
    fn = calls[0].get("function") or {}
    raw = fn.get("arguments") or ""
    if isinstance(raw, dict):
        return fn.get("name", ""), raw, json.dumps(raw)
    try:
        args = json.loads(raw)
    except ValueError:
        args = None
    return fn.get("name", ""), args if isinstance(args, dict) else None, raw


def error_text(data: Any) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return clip(err.get("message", err))
        if err:
            return clip(err)
        if "_raw" in data:
            return clip(data["_raw"])
    return ""


CAPITAL_SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
                  "required": ["city", "country"], "additionalProperties": False}
CAPITAL_PROMPT = "What is the capital of France? Reply as a JSON object with the keys city and country."


def response_format(fmt: str, schema: dict | None = None, name: str = "capital") -> dict:
    if fmt == "json_object":
        return {"type": "json_object"}
    return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema or CAPITAL_SCHEMA}}


def structured_problem(status: int, data: Any, fmt: str) -> str:
    """'' when a structured-output response is a 200 carrying the JSON asked for."""
    if status != 200:
        return f"HTTP {status} {error_text(data)}".strip()
    content = message_of(data).get("content") or ""
    try:
        obj = json.loads(content)
    except ValueError:
        return f"content is not JSON: {clip(content, 80)!r}"
    if not isinstance(obj, dict):
        return "content is JSON but not an object"
    if fmt == "json_schema" and not all(isinstance(obj.get(k), str) for k in ("city", "country")):
        return f"content does not match the schema: {clip(content, 80)!r}"
    return ""


def grammar_schema(min_length: int, objects: int) -> dict:
    """A json_schema as large as a full tool manifest, plus one string longer
    than llama.cpp's stock MAX_REPETITION_THRESHOLD (2000): patch 0001."""
    props: dict[str, Any] = {"summary": {"type": "string", "minLength": min_length}}
    for i in range(objects):
        props[f"tool_{i:03d}"] = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": [f"tool_{i:03d}"]},
                "query": {"type": "string", "maxLength": 512},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                "mode": {"type": "string", "enum": ["fast", "full", "dry-run"]},
                "strict": {"type": "boolean"},
            },
            "required": ["name", "query"],
        }
    return {"type": "object", "properties": props, "required": ["summary"]}


WEATHER_TOOL = {"type": "function", "function": {
    "name": "get_weather", "description": "Get the current weather for a city.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string", "description": "The city name"}},
                   "required": ["city"]}}}
CAPITAL_TOOL = {"type": "function", "function": {
    "name": "lookup_capital", "description": "Look up the capital city of a country.",
    "parameters": {"type": "object", "properties": {"country": {"type": "string", "description": "The country"}},
                   "required": ["country"]}}}
_FILLER = ("inventory warehouse shipment pallet supplier invoice ledger region quarterly forecast audit "
           "reconciliation barcode carrier customs manifest dock freight batch expiry recall").split()


def decoy_tool(i: int) -> dict:
    desc = " ".join(_FILLER[(i + j) % len(_FILLER)] for j in range(90))
    return {"type": "function", "function": {
        "name": f"inventory_lookup_{i:03d}",
        "description": f"Decoy tool {i}: look up {desc}.",
        "parameters": {"type": "object", "properties": {
            "sku": {"type": "string", "description": f"Stock keeping unit for record set {i}, " + " ".join(_FILLER[:12])},
            "warehouse": {"type": "string", "description": "Warehouse code, " + " ".join(_FILLER[5:15])},
            "include_history": {"type": "boolean", "description": "Include movement history"},
            "limit": {"type": "integer", "description": "Maximum rows to return"},
        }, "required": ["sku"]}}}


def large_tool_array(count: int) -> list[dict]:
    """`count` deterministic decoy tools, then the target (lookup_capital) last."""
    return [decoy_tool(i) for i in range(count)] + [CAPITAL_TOOL]


def tool_count_for(min_tokens: int, tokens_per_tool: float | None) -> int:
    """Decoys needed for min_tokens with 15% margin. Without a measurement
    (the server has no /tokenize), assume a dense 3 characters per token."""
    per = tokens_per_tool or len(json.dumps(decoy_tool(0))) / 3.0
    return math.ceil(min_tokens * 1.15 / per)


CONCURRENCY_PROMPTS = [("The capital of France is", "Paris"), ("The capital of Japan is", "Tokyo"),
                       ("The capital of Italy is", "Rome"), ("The capital of Egypt is", "Cairo"),
                       ("The capital of Spain is", "Madrid"), ("The capital of Kenya is", "Nairobi"),
                       ("The capital of Peru is", "Lima"), ("The capital of Norway is", "Oslo")]
MTP_PROMPT = ("Write a detailed, factual essay about the history of Paris, from its founding "
              "as a Roman settlement to the present day.")


def comfy_workflow(ckpt: str, width: int, height: int, steps: int) -> dict:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "a lighthouse on a cliff at sunset, photo", "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry, low quality", "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": steps, "cfg": 7.0, "sampler_name": "euler",
                                                   "scheduler": "normal", "denoise": 1.0, "model": ["1", 0],
                                                   "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "hal0-gate", "images": ["6", 0]}},
    }


# ---- a minimal WebSocket client (RFC 6455), for the STT stream probe -------

def ws_frame(opcode: int, payload: bytes, mask: bytes) -> bytes:
    """One final, masked client frame."""
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def ws_parse(buf: bytes) -> tuple[int, bytes, int] | None:
    """(opcode, payload, bytes used) of the first complete server frame, or None."""
    if len(buf) < 2:
        return None
    opcode, n, pos = buf[0] & 0x0F, buf[1] & 0x7F, 2
    if n == 126:
        if len(buf) < 4:
            return None
        n, pos = struct.unpack(">H", buf[2:4])[0], 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n, pos = struct.unpack(">Q", buf[2:10])[0], 10
    if buf[1] & 0x80:
        mask, pos = buf[pos:pos + 4], pos + 4
    else:
        mask = b""
    if len(buf) < pos + n:
        return None
    payload = buf[pos:pos + n]
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload, pos + n


def ws_stream_partial(port: int, path: str, pcm: bytes, timeout: float) -> str:
    """Send PCM16 in 0.5 s frames; return the first non-empty partial transcript."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise Fail("the WebSocket handshake was closed")
            buf += chunk
        head, buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise Fail(f"WebSocket upgrade refused: {clip(head.decode(errors='replace'), 80)}")
        step = 16000  # 0.5 s of 16 kHz 16-bit mono
        for i in range(0, len(pcm), step):
            sock.sendall(ws_frame(0x2, pcm[i:i + step], os.urandom(4)))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = ws_parse(buf)
            if frame is None:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                continue
            opcode, payload, used = frame
            buf = buf[used:]
            if opcode == 0x8:
                break
            if opcode == 0x1:
                msg = json.loads(payload)
                if msg.get("error"):
                    raise Fail(f"stream error: {clip(msg['error'])}")
                if str(msg.get("text", "")).strip():
                    return str(msg["text"])
        raise Fail("no partial transcript arrived")
    finally:
        try:
            sock.sendall(ws_frame(0x8, b"", os.urandom(4)))
        except OSError:
            pass
        sock.close()


# ---------------------------------------------------------------------------
# Results and the report (pure, tested)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Result:
    name: str
    lane: str | None
    status: str  # "passed" | "failed" | "skipped"
    detail: str
    informational: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.lane}/{self.name}" if self.lane else self.name


def allowed(name: str, patterns: list[str]) -> bool:
    short = name.split("/", 1)[-1]
    return any(fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(short, p) for p in patterns)


def build_report(*, key: str, image: str, digest: str, tag: str, results: list[Result], lanes: list[str],
                 allow_skip: list[str], date: str, models: list[dict], revision: str) -> dict:
    """One runner's hal0.gate-report.v1 document (docs/UPDATING.md section 4).

    `probes` lists every gating probe; a probe skipped without --allow-skip is
    in it with passed false. Skips that --allow-skip covers are listed under
    `skipped`, informational probes under `informational`; neither gates. A
    lane passed when at least one of its probes ran and all of them passed.
    """
    probes: list[dict] = []
    skipped: list[dict] = []
    info: list[dict] = []
    for r in results:
        name, detail = r.full_name, clip(r.detail, DETAIL_MAX)
        if r.informational:
            info.append({"name": name, "status": r.status, "detail": detail})
        elif r.status == "skipped" and allowed(name, allow_skip):
            skipped.append({"name": name, "reason": detail})
        else:
            entry: dict[str, Any] = {"name": name, "passed": r.status == "passed", "detail": detail}
            if r.status == "skipped":
                entry["skipped"] = True
            probes.append(entry)
    lane_doc: dict[str, dict] = {}
    for lane in lanes:
        mine = [p for p in probes if p["name"].startswith(f"{lane}/")]
        lane_doc[lane] = {"passed": bool(mine) and all(p["passed"] for p in mine), "probes": len(mine)}
    passed = (bool(probes) and all(p["passed"] for p in probes)
              and any(v["passed"] for v in lane_doc.values()))
    return {
        "schema": SCHEMA, "runner": key, "image": image, "digest": digest, "tag": tag,
        "passed": passed, "date": date, "lanes": lane_doc, "probes": probes,
        "skipped": skipped, "informational": info, "models": models,
        "gate": {"script": "scripts/gate.py", "revision": revision, "allow_skip": list(allow_skip)},
    }


def group_report(reports: list[dict], date: str) -> dict:
    return {"schema": SCHEMA, "passed": all(r["passed"] for r in reports), "date": date,
            "runners": [{k: v for k, v in r.items() if k != "schema"} for r in reports]}


def self_check(doc: dict, digests: dict[str, str]) -> None:
    """Run promote's validator on what we are about to publish: a passing report
    must be accepted, a failing one refused. Disagreement is a bug here."""
    for key, digest in digests.items():
        try:
            mine = promote.report_for(doc, key)
        except SystemExit as e:
            raise GateError(f"internal: promote cannot read the report for {key}: {e}")
        try:
            promote.validate_gate_report(mine, key, digest)
            accepted = True
        except SystemExit:
            accepted = False
        if accepted != (mine.get("passed") is True):
            raise GateError(f"internal: promote {'accepts' if accepted else 'refuses'} the {key} report, "
                            f"which says passed={mine.get('passed')}")


PRIVATE_ADDR = re.compile(r"(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01])|169\.254|"
                          r"100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\.\d{1,3}\.\d{1,3}(?![\d.])")
GENERIC_HOSTS = {"localhost", "hal0", "ubuntu", "fedora", "debian"}


def scrub(obj: Any, secrets: list[str], hostnames: list[str]) -> Any:
    """Remove operator paths, host names and private addresses from every string."""
    secrets = sorted({s for s in secrets if s and len(s) > 3 and s != "/"}, key=len, reverse=True)
    hosts = [h for h in {*hostnames} if h and len(h) > 2 and h.lower() not in GENERIC_HOSTS]

    def one(s: str) -> str:
        for sec in secrets:
            s = s.replace(sec, "<host-path>")
        for h in hosts:
            s = re.sub(rf"(?<![\w-]){re.escape(h)}(?![\w-])", "<host>", s, flags=re.I)
        return PRIVATE_ADDR.sub("<addr>", s)

    if isinstance(obj, str):
        return one(obj)
    if isinstance(obj, list):
        return [scrub(x, secrets, hosts) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub(v, secrets, hosts) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# Registry and upload
# ---------------------------------------------------------------------------

MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"])


def registry_digest(image: str, tag: str, urlopen: Callable = urllib.request.urlopen) -> str:
    """The digest `image:tag` resolves to now, asked of the registry anonymously."""
    host, _, repo = image.partition("/")
    with urlopen(urllib.request.Request(f"https://{host}/token?scope=repository:{repo}:pull&service={host}"),
                 timeout=30) as r:
        token = json.loads(r.read())["token"]
    req = urllib.request.Request(f"https://{host}/v2/{repo}/manifests/{tag}", method="HEAD",
                                 headers={"Accept": MANIFEST_ACCEPT, "Authorization": f"Bearer {token}"})
    with urlopen(req, timeout=30) as r:
        digest = r.headers.get("Docker-Content-Digest", "")
    if not DIGEST_RE.match(digest):
        raise GateError(f"the registry returned no digest for {image}:{tag}")
    return digest


def release_tag(subject: str, tag: str) -> str:
    return f"gate-{subject}-{tag}"


def asset_url(release: str, asset: str) -> str:
    return f"https://github.com/{REPO_SLUG}/releases/download/{release}/{asset}"


def upload(path: Path, release: str, title: str, run: Callable = subprocess.run, gh: str = "gh") -> str:
    """Attach the report to the `release` prerelease of this repository (created
    if missing, never marked latest); asset names are unique, nothing is replaced."""
    def call(argv: list[str]) -> subprocess.CompletedProcess:
        return run(argv, capture_output=True, text=True)

    if call([gh, "release", "view", release, "--repo", REPO_SLUG, "--json", "tagName"]).returncode != 0:
        notes = ("Hardware gate report(s) written by scripts/gate.py (docs/UPDATING.md section 4). "
                 "Pass the asset URL to the Promote runner image workflow as gate_reports.")
        cp = call([gh, "release", "create", release, "--repo", REPO_SLUG, "--target", "main", "--title", title,
                   "--notes", notes, "--prerelease", "--latest=false"])
        if cp.returncode != 0:
            raise GateError(f"gh release create {release} failed: {clip(cp.stderr or cp.stdout)}")
    cp = call([gh, "release", "upload", release, str(path), "--repo", REPO_SLUG])
    if cp.returncode != 0:
        raise GateError(f"gh release upload failed: {clip(cp.stderr or cp.stdout)}")
    return asset_url(release, path.name)


def fetch_public(url: str, tries: int = 5, wait: float = 3.0) -> Any:
    """GET without credentials, the way anyone (and promote) will read it."""
    last: Exception | None = None
    for _ in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}),
                                        timeout=30) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
            time.sleep(wait)
    raise GateError(f"the uploaded report is not readable at {url}: {last}")


# ---------------------------------------------------------------------------
# The host: podman, memory, devices, HTTP (replaced by fakes in the tests)
# ---------------------------------------------------------------------------

class Http:
    def __init__(self, port: int) -> None:
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
                timeout: float = 60) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            return 0, str(e).encode()

    def get(self, path: str, timeout: float = 30) -> tuple[int, bytes]:
        return self.request("GET", path, timeout=timeout)

    def post_json(self, path: str, obj: Any, timeout: float = 600) -> tuple[int, Any]:
        st, raw = self.request("POST", path, json.dumps(obj).encode(), {"Content-Type": "application/json"}, timeout)
        try:
            return st, json.loads(raw)
        except ValueError:
            return st, {"_raw": raw[:300].decode(errors="replace")}

    def post_raw(self, path: str, body: bytes, ctype: str, timeout: float = 600) -> tuple[int, bytes]:
        return self.request("POST", path, body, {"Content-Type": ctype}, timeout)

    def stream_partial(self, path: str, pcm: bytes, timeout: float) -> str:
        return ws_stream_partial(self.port, path, pcm, timeout)


class Host:
    def __init__(self, podman: str = "podman", verbose: bool = True) -> None:
        self.podman_bin = podman
        self.verbose = verbose
        self.containers: set[str] = set()
        self._tmp: str | None = None

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"gate: {msg}", file=sys.stderr, flush=True)

    def podman(self, argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
        """Run podman; stdout and stderr together, except for the commands whose
        output is parsed (inspect, ps), where warnings on stderr would corrupt it."""
        parsed = len(argv) > 1 and argv[1] in ("inspect", "ps", "image")
        try:
            return subprocess.run(argv, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL if parsed else subprocess.STDOUT,
                                  text=True, errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as e:
            out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode(errors="replace")
            return subprocess.CompletedProcess(argv, 124, out + f"\n[gate: timed out after {timeout:.0f} s]")
        except FileNotFoundError:
            raise GateError(f"{argv[0]} is not installed")

    def memory(self) -> tuple[int, str]:
        def read(p: str) -> str | None:
            try:
                return Path(p).read_text()
            except OSError:
                return None
        return available_memory(read(NODE_MEMINFO), read("/proc/meminfo"))

    def devices(self, devs: list[str]) -> tuple[list[str], list[str]]:
        return expand_devices(devs)

    def groups(self) -> list[str]:
        gids = {}
        for g in ("video", "render"):
            try:
                gids[g] = grp.getgrnam(g).gr_gid
            except KeyError:
                pass
        return group_args(os.geteuid(), gids)

    def free_port(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def http(self, port: int) -> Http:
        return Http(port)

    def tmpdir(self) -> Path:
        if self._tmp is None:
            self._tmp = tempfile.mkdtemp(prefix="hal0-gate-")
        return Path(self._tmp)

    def sleep(self, s: float) -> None:
        time.sleep(s)

    def now(self) -> float:
        return time.monotonic()

    def size(self, path: str) -> int:
        p = Path(path)
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        return p.stat().st_size

    def is_dir(self, path: str) -> bool:
        return Path(path).is_dir()

    def read_bytes(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OverflowError):
            return True
        return True

    def lock(self) -> None:
        """One gate at a time on this host: two would load two models at once."""
        self._lock = open_lock(os.environ.get(LOCK_ENV) or LOCK_FILE)

    def cleanup(self) -> None:
        for name in sorted(self.containers):
            self.podman([self.podman_bin, "rm", "-f", "-t", "5", name], timeout=120)
        self.containers.clear()
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None
        if getattr(self, "_lock", None) is not None:
            os.close(self._lock)  # releases the flock
            self._lock = None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class SessionCtx:
    gate: "Gate"
    lane: str | None
    session: dict
    args: list[str]
    client: Any
    container: str
    preload_available: int
    lane_state: dict

    def logs(self) -> str:
        return self.gate.logs(self.container)

    def render(self, text: str) -> str:
        return render(text, self.gate.resolver(set()))


class Gate:
    def __init__(self, key: str, digest: str, tag: str | None, models: Models, host: Host,
                 headroom_gib: float = DEFAULT_HEADROOM_GIB, plan_only: bool = False) -> None:
        self.key, self.digest, self.tag = key, digest, tag
        self.doc, self.ver = load_plan(key)
        self.image = self.doc["image"]["package"]
        self.ref = f"{self.image}@{digest}"
        self.models, self.host = models, host
        self.headroom = int(headroom_gib * GIB)
        self.plan_only = plan_only
        self.seq = 0
        self.used_models: dict[str, dict] = {}
        self.lane_state: dict[str, dict] = {}

    # ---- placeholders, mounts, memory ----------------------------------

    def resolver(self, garbage_used: set) -> Callable[[str, str, str], str]:
        src = self.doc["source"]
        version = str(self.doc["image"]["version"])

        def resolve(kind: str, key: str, field: str) -> str:
            if kind == "path":
                return self.container_path(key)
            if kind == "value":
                if self.plan_only and key not in self.models.entries:
                    return f"<{key}:{field}>"
                return self.models.value(key, field)
            if kind == "version":
                return version
            if kind == "version_digits":
                return re.sub(r"\D", "", version)
            if kind == "ref7":
                return str(src.get("ref", ""))[:7]
            if kind == "garbage":
                garbage_used.add(True)
                return GARBAGE_TARGET
            raise Fail(f"unknown placeholder {kind}")
        return resolve

    def host_path(self, key: str) -> str:
        if self.plan_only:
            return self.models.entries.get(key, {}).get("path") or f"<{key}>"
        return self.models.path(key)

    def container_path(self, key: str, target: str | None = None) -> str:
        hp = self.host_path(key)
        is_dir = False if hp.startswith("<") else self.host.is_dir(hp)
        if target:
            return target.replace("{basename}", Path(hp).name)
        return default_target(key, hp, is_dir)

    def mounts_for(self, texts: list[str], explicit: list[dict], garbage: bool) -> list[tuple[str, str, bool]]:
        mounts: list[tuple[str, str, bool]] = []
        for key in path_keys(texts):
            mounts.append((self.host_path(key), self.container_path(key), True))
        for m in explicit:
            mounts.append((self.host_path(m["key"]), self.container_path(m["key"], m["target"]), True))
        if garbage:
            if self.plan_only:
                mounts.append(("<garbage file>", GARBAGE_TARGET, True))
            else:
                f = self.host.tmpdir() / "not-a-model.gguf"
                f.write_text("this file is deliberately not a GGUF model\n" * 64)
                f.chmod(0o644)
                mounts.append((str(f), GARBAGE_TARGET, True))
        by_path = {e.get("path"): k for k, e in self.models.entries.items() if e.get("path")}
        for host, _, _ in mounts:
            if host in by_path and not self.plan_only:
                key = by_path[host]
                self.used_models[key] = {"key": key, "file": Path(host).name, "bytes": self.host.size(host)}
        return mounts

    def need_bytes(self, mounts: list[tuple[str, str, bool]], extra_gib: float, fixed_gib: float | None) -> int:
        if fixed_gib is not None:
            return int(fixed_gib * GIB)
        return sum(self.host.size(h) for h, _, _ in mounts) + int(extra_gib * GIB)

    def memory_gate(self, need: int) -> int:
        avail, source = self.host.memory()
        ok, detail = headroom_decision(avail, need, self.headroom)
        if not ok:
            raise Skip(f"insufficient memory ({source}): {detail}")
        return avail

    def lane_devices(self, spec: dict) -> list[str]:
        if self.plan_only or not spec.get("devices"):
            return list(spec.get("devices", []))
        devices, missing = self.host.devices(spec["devices"])
        if missing:
            raise Skip(f"device {', '.join(missing)} is not present")
        return devices

    def lane_spec(self, lane: str | None) -> dict:
        return self.ver["lanes"].get(lane, {}) if lane else {}

    def container_name(self) -> str:
        self.seq += 1
        return f"gate-{self.key}-{os.getpid()}-{self.seq}"

    # ---- container lifecycle -------------------------------------------

    def podman_cmd(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
        return self.host.podman([self.host.podman_bin, *args], timeout=timeout)

    def remove(self, name: str) -> None:
        self.podman_cmd("rm", "-f", "-t", "5", name)
        self.host.containers.discard(name)

    def logs(self, name: str) -> str:
        return self.podman_cmd("logs", name, timeout=60).stdout or ""

    def state(self, name: str) -> tuple[bool, str]:
        cp = self.podman_cmd("inspect", "--format", "{{.State.Running}} {{.State.ExitCode}}", name)
        parts = (cp.stdout or "").split()
        return (bool(parts) and parts[0] == "true"), (parts[1] if len(parts) > 1 else "?")

    def remove_stale(self) -> None:
        """Containers a killed gate run of this runner left behind: named
        gate-<key>-<pid>-<n> by a process that no longer exists. A live run's
        containers are never touched (and the host lock keeps runs apart)."""
        cp = self.podman_cmd("ps", "-a", "--format", "{{.Names}}", "--filter", f"name=^gate-{self.key}-")
        for name in (cp.stdout or "").split():
            m = re.match(rf"^gate-{re.escape(self.key)}-(\d+)-\d+$", name)
            if m and not self.host.pid_alive(int(m.group(1))):
                self.host.log(f"removing a container left by an earlier gate run: {name}")
                self.remove(name)

    # ---- image ----------------------------------------------------------

    def verify_image(self) -> Result:
        self.host.log(f"pulling {self.ref}")
        cp = self.podman_cmd("pull", self.ref, timeout=7200)
        if cp.returncode != 0:
            raise GateError(f"podman pull {self.ref} failed: {clip(cp.stdout, 300)}")
        cp = self.podman_cmd("image", "inspect", "--format", "{{json .Labels}}", self.ref)
        try:
            labels = json.loads(cp.stdout) or {}
        except ValueError:
            raise GateError(f"cannot read the labels of {self.ref}: {clip(cp.stdout)}")
        problems = []
        if labels.get(KEY_LABEL) != self.key:
            problems.append(f"label {KEY_LABEL}={labels.get(KEY_LABEL)!r}, not {self.key!r}")
        label_tag = labels.get(TAG_LABEL, "")
        if self.tag and label_tag != self.tag:
            problems.append(f"label {TAG_LABEL}={label_tag!r}, not the tag given ({self.tag})")
        self.tag = self.tag or label_tag
        if not promote.lib.TAG_RE.match(self.tag or ""):
            problems.append(f"{self.tag!r} is not an immutable <version>-r<sha7> tag")
        else:
            try:
                now = registry_digest(self.image, self.tag)
            except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
                raise GateError(f"cannot ask the registry what {self.image}:{self.tag} points at: {e}")
            if now != self.digest:
                problems.append(f"{self.image}:{self.tag} now points at {now}, not {self.digest}")
        detail = "; ".join(problems) or f"{self.image}:{self.tag} resolves to {self.digest}; labels match"
        return Result("image", None, "failed" if problems else "passed", detail)

    # ---- probes -----------------------------------------------------------

    def run_exec(self, p: dict, lane: str | None, args: list[str] | None = None,
                 entrypoint: str | None = None, timeout: float | None = None) -> str:
        """Run one container to completion; return its output. Raises Skip / Fail."""
        spec = self.lane_spec(lane)
        args = list(p.get("args", []) if args is None else args)
        if p.get("lane_args"):
            args += spec.get("server_args", [])
        garbage: set = set()
        rendered = [render(a, self.resolver(garbage)) for a in args]
        mounts = self.mounts_for(args, [], bool(garbage))
        devices: list[str] = []
        if lane and not p.get("no_devices"):
            devices = self.lane_devices(spec)
        name = self.container_name()
        argv = podman_run_argv(podman=self.host.podman_bin, name=name, image=self.ref, args=rendered,
                               entrypoint=entrypoint or p.get("entrypoint"), devices=devices,
                               groups=self.host.groups() if devices else [],
                               podman_args=spec.get("podman_args", []) if lane else [], mounts=mounts)
        if self.plan_only:
            print("    " + " ".join(argv))
            return ""
        real = [m for m in mounts if m[1] != GARBAGE_TARGET]
        if real or p.get("mem_gib"):
            self.memory_gate(self.need_bytes(real, 1.0, p.get("mem_gib")))
        self.host.containers.add(name)
        try:
            cp = self.host.podman(argv, timeout=timeout or p.get("timeout", 600))
        finally:
            self.remove(name)
        out = cp.stdout or ""
        want = p.get("exit", 0)
        if cp.returncode != want:
            raise Fail(f"exit {cp.returncode}, want {want}: {clip(out[-400:], 300)}")
        return out

    def probe_exec(self, p: dict, lane: str | None) -> str:
        out = self.run_exec(p, lane)
        if self.plan_only:
            return ""
        res = self.resolver(set())
        missing = [pat for pat in p.get("expect", []) if not re.search(render(pat, res, regex=True), out)]
        found = [pat for pat in p.get("reject", []) if re.search(render(pat, res, regex=True), out)]
        if missing or found:
            raise Fail("; ".join([f"output lacks /{m}/" for m in missing] + [f"output has /{f}/" for f in found])
                       + f": {clip(out[-300:], 200)}")
        return f"exit {p.get('exit', 0)}: {clip(out.strip().splitlines()[0] if out.strip() else '', 200)}"

    def probe_perplexity(self, p: dict) -> str:
        values = {}
        for lane in p["compare"]:
            spec = self.lane_spec(lane)
            args = ["-m", f"{{path:{p['model']}}}", "-f", f"{{path:{p['text']}}}", "-c", str(p.get("ctx", 512)),
                    "--chunks", str(p.get("chunks", 20))] + spec.get("server_args", [])
            if self.plan_only:
                print(f"  [{lane}]")
            out = self.run_exec({"mem_gib": None}, lane, args=args,
                                entrypoint=p.get("entrypoint", "/opt/hal0/bin/llama-perplexity"),
                                timeout=p.get("timeout", 3600))
            if self.plan_only:
                continue
            ppl = parse_ppl(out)
            if ppl is None:
                raise Fail(f"{lane}: no 'Final estimate: PPL' in the output: {clip(out[-300:], 200)}")
            values[lane] = ppl
        if self.plan_only:
            return ""
        a, b = (values[k] for k in p["compare"])
        diff = abs(a - b) / min(a, b) * 100
        detail = ", ".join(f"{k} PPL {v:.4f}" for k, v in values.items()) + f"; {diff:.2f}% apart"
        if diff > p.get("max_diff_pct", 1.0):
            raise Fail(detail + f" (limit {p.get('max_diff_pct', 1.0)}%)")
        return detail

    def run_probe(self, p: dict, lane: str | None) -> Result:
        kind = p["kind"]
        if self.plan_only:
            print(f"  probe {p['name']} ({kind})" + (f" [{lane}]" if lane else ""))
        if kind == "elsewhere":
            return Result(p["name"], lane, "skipped", f"not run by gate.py: {p['doc']}", informational=True)
        try:
            detail = self.probe_perplexity(p) if kind == "perplexity" else self.probe_exec(p, lane)
            return Result(p["name"], lane, "passed", detail)
        except Skip as e:
            return Result(p["name"], lane, "skipped", f"skipped: {e}")
        except Fail as e:
            return Result(p["name"], lane, "failed", str(e))

    # ---- sessions ---------------------------------------------------------

    def run_session(self, s: dict, lane: str | None) -> list[Result]:
        checks = s["check"]
        informational = bool(s.get("informational"))

        def all_checks(status: str, detail: str) -> list[Result]:
            return [Result(c["name"], lane, status, detail, informational or bool(c.get("informational")))
                    for c in checks]

        spec = self.lane_spec(lane)
        if self.plan_only:
            print(f"  session {s['name']}" + (f" [{lane}]" if lane else "")
                  + f": checks {', '.join(c['name'] for c in checks)}")
        try:
            args = list(s.get("args", [])) + spec.get("server_args", [])
            rendered = [render(a, self.resolver(set())) for a in args]
            mounts = self.mounts_for(args, s.get("mounts", []), False)
            for c in s.get("cache", []):
                if c["key"] in self.models.entries:
                    mounts.append((self.host_path(c["key"]), c["target"], False))
                elif self.plan_only:
                    mounts.append(("<new empty directory>", c["target"], False))
                else:
                    d = Path(tempfile.mkdtemp(prefix="cache-", dir=self.host.tmpdir()))
                    d.chmod(0o777)
                    mounts.append((str(d), c["target"], False))
            devices = self.lane_devices(spec)
            ro = [m for m in mounts if m[2]]
            name = self.container_name()
            port = s["port"]
            hostport = 0 if self.plan_only else self.host.free_port()
            argv = podman_run_argv(podman=self.host.podman_bin, name=name, image=self.ref, args=rendered,
                                   entrypoint=s.get("entrypoint"), devices=devices,
                                   groups=self.host.groups() if devices else [],
                                   podman_args=spec.get("podman_args", []), mounts=mounts,
                                   env=s.get("env"), publish=(hostport or port, port), detach=True)
            if self.plan_only:
                print("    " + " ".join(argv))
                return all_checks("skipped", "plan only")
            avail = self.memory_gate(self.need_bytes(ro, s.get("mem_extra_gib", 2.0), s.get("mem_gib")))
        except Skip as e:
            return all_checks("skipped", f"skipped: {e}")

        self.host.log(f"[{lane or '-'}] session {s['name']}: starting {name}")
        self.host.containers.add(name)
        try:
            cp = self.host.podman(argv, timeout=600)
            if cp.returncode != 0:
                return all_checks("failed", f"podman run failed: {clip(cp.stdout, 300)}")
            client = self.host.http(hostport)
            try:
                self.wait_ready(name, client, s)
            except Fail as e:
                return all_checks("failed", f"session {s['name']} did not start: {e}")
            except Skip as e:
                return all_checks("skipped", f"skipped: {e}")
            ctx = SessionCtx(self, lane, s, args, client, name, avail,
                             self.lane_state.setdefault(lane or "", {}))
            results = []
            for c in checks:
                self.host.log(f"[{lane or '-'}] {s['name']}/{c['name']}")
                info = informational or bool(c.get("informational"))
                try:
                    ok, detail = CHECKS[c["kind"]](ctx, c)
                    results.append(Result(c["name"], lane, "passed" if ok else "failed", detail, info))
                except Skip as e:
                    results.append(Result(c["name"], lane, "skipped", f"skipped: {e}", info))
                except Fail as e:
                    results.append(Result(c["name"], lane, "failed", str(e), info))
                except Exception as e:  # noqa: BLE001  a probe bug is a failed probe, not a crash
                    results.append(Result(c["name"], lane, "failed", f"{type(e).__name__}: {e}", info))
            return results
        finally:
            self.remove(name)

    def wait_ready(self, name: str, client: Any, s: dict) -> None:
        path = s.get("health", "/health")
        ready = s.get("ready") or {}
        deadline = self.host.now() + s.get("startup_timeout", 900)
        floor = self.headroom // 2
        while True:
            st, raw = client.get(path, timeout=10)
            if st == 200:
                try:
                    data = json.loads(raw) if ready else {}
                except ValueError:
                    data = {}
                if all(data.get(k) == v for k, v in ready.items()):
                    return
            running, code = self.state(name)
            if not running:
                raise Fail(f"the container exited ({code}) before {path} answered: "
                           f"{clip(self.logs(name)[-600:], 400)}")
            avail, _ = self.host.memory()
            if avail < floor:
                self.remove(name)
                raise Fail(f"stopped: available memory fell to {gib(avail)} during the load (floor {gib(floor)})")
            if self.host.now() > deadline:
                raise Fail(f"{path} not ready after {s.get('startup_timeout', 900)} s: {clip(self.logs(name)[-400:], 300)}")
            self.host.sleep(2)

    # ---- the whole runner -------------------------------------------------

    def items(self) -> tuple[list[dict], dict[str, list[tuple[str, dict]]], list[dict]]:
        """(lane-less probes, per-lane items in manifest order, cross-lane probes)."""
        lanes = list(self.ver["lanes"])
        laneless, cross = [], []
        per_lane: dict[str, list[tuple[str, dict]]] = {lane: [] for lane in lanes}
        for p in self.ver.get("probe", []):
            if p["kind"] in ("perplexity", "elsewhere"):
                cross.append(p)
            elif p.get("lanes") == []:
                laneless.append(p)
            else:
                for lane in p.get("lanes", lanes):
                    per_lane[lane].append(("probe", p))
        for s in self.ver.get("session", []):
            for lane in s.get("lanes", lanes):
                per_lane[lane].append(("session", s))
        return laneless, per_lane, cross

    def run(self) -> list[Result]:
        results: list[Result] = []
        if not self.plan_only:
            self.remove_stale()
            image = self.verify_image()
            results.append(image)
            if image.status != "passed":
                return results
        laneless, per_lane, cross = self.items()
        for p in laneless:
            results.append(self.run_probe(p, None))
        for lane, items in per_lane.items():
            if self.plan_only:
                print(f" lane {lane}")
            for kind, item in items:
                results.extend([self.run_probe(item, lane)] if kind == "probe" else self.run_session(item, lane))
        for p in cross:
            results.append(self.run_probe(p, None))
        return results


# ---------------------------------------------------------------------------
# Session checks: (ctx, params) -> (passed, detail)
# ---------------------------------------------------------------------------

def chk_http_json(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    path = c.get("path") or sc.session.get("health", "/health")
    if c.get("no_port_arg") and any(a == "--port" or a.startswith("--port=") for a in sc.args):
        return False, "the session passes --port, so it cannot show the default port"
    st, raw = sc.client.get(path)
    if st != 200:
        return False, f"GET {path} -> {st}: {clip(raw.decode(errors='replace'), 120)}"
    problems = []
    if c.get("equals") or c.get("contains") or c.get("nonempty"):
        try:
            data = json.loads(raw)
        except ValueError:
            return False, f"GET {path}: not JSON"
        for k, v in (c.get("equals") or {}).items():
            if data.get(k) != v:
                problems.append(f"{k}={data.get(k)!r}, want {v!r}")
        for k, v in (c.get("contains") or {}).items():
            if str(v) not in str(data.get(k, "")):
                problems.append(f"{k}={data.get(k)!r} lacks {v!r}")
        for k in c.get("nonempty") or []:
            if not data.get(k):
                problems.append(f"{k} is empty")
    port_note = f" on container port {sc.session['port']} with no --port argument" if c.get("no_port_arg") else ""
    return not problems, "; ".join(problems) or f"GET {path} -> 200{port_note}"


def chk_completion(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    st, data = sc.client.post_json("/completion", {"prompt": c["prompt"], "n_predict": c.get("n_predict", 16),
                                                   "temperature": 0, "cache_prompt": False},
                                   timeout=c.get("timeout", 600))
    text = data.get("content", "") if isinstance(data, dict) else ""
    ok = st == 200 and c["expect"].lower() in text.lower()
    return ok, f"HTTP {st}: {c['prompt']!r} -> {clip(text, 100)!r}" + ("" if ok else f" (want {c['expect']!r})")


def chk_chat(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    body: dict[str, Any] = {"messages": [{"role": "user", "content": c["prompt"]}], "temperature": 0,
                            "max_tokens": c.get("max_tokens", 256)}
    if c.get("model"):
        body["model"] = sc.render(c["model"])
    if "thinking" in c:
        body["chat_template_kwargs"] = {"enable_thinking": bool(c["thinking"])}
    st, data = sc.client.post_json("/v1/chat/completions", body, timeout=c.get("timeout", 600))
    text = message_of(data).get("content") or ""
    ok = st == 200 and c["expect"].lower() in text.lower()
    return ok, f"HTTP {st}: {clip(text, 100)!r}" + ("" if ok else f" (want {c['expect']!r}) {error_text(data)}")


def chk_structured(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    parts, ok = [], True
    for fmt in c.get("formats", ["json_object", "json_schema"]):
        for think in c.get("thinking", ["absent"]):
            body: dict[str, Any] = {"messages": [{"role": "user", "content": CAPITAL_PROMPT}], "temperature": 0,
                                    "max_tokens": c.get("max_tokens", 4096), "response_format": response_format(fmt)}
            if think != "absent":
                body["chat_template_kwargs"] = {"enable_thinking": think == "true"}
            st, data = sc.client.post_json("/v1/chat/completions", body, timeout=c.get("timeout", 900))
            problem = structured_problem(st, data, fmt)
            ok = ok and not problem
            parts.append(f"{fmt}/thinking={think}: {problem or 'ok'}")
    return ok, "; ".join(parts)


def chk_grammar_size(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    schema = grammar_schema(c.get("min_length", 2500), c.get("objects", 80))
    body = {"messages": [{"role": "user", "content": "Summarise the tools as JSON."}], "temperature": 0,
            "max_tokens": 16, "chat_template_kwargs": {"enable_thinking": False},
            "response_format": response_format("json_schema", schema, "tool_manifest")}
    st, data = sc.client.post_json("/v1/chat/completions", body, timeout=c.get("timeout", 600))
    size = len(json.dumps(schema))
    return st == 200, f"json_schema of {size} bytes with minLength {c.get('min_length', 2500)} -> HTTP {st} {error_text(data)}".strip()


def chk_tool_call(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    body = {"messages": [{"role": "user", "content": "What is the weather in Paris right now? Use the get_weather tool."}],
            "tools": [WEATHER_TOOL], "tool_choice": "auto", "temperature": 0, "max_tokens": c.get("max_tokens", 4096)}
    st, data = sc.client.post_json("/v1/chat/completions", body, timeout=c.get("timeout", 900))
    name, args, raw = first_tool_call(data)
    ok = st == 200 and name == "get_weather" and args is not None and "paris" in str(args.get("city", "")).lower()
    return ok, f"HTTP {st}: tool_calls[0] = {name or 'none'}({clip(raw, 80)}) {error_text(data)}".strip()


def chk_large_tools(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    need = c.get("min_prompt_tokens", 29000)
    sample = [decoy_tool(i) for i in range(20)]
    st, tok = sc.client.post_json("/tokenize", {"content": json.dumps(sample)}, timeout=120)
    per = len(tok.get("tokens", [])) / len(sample) if st == 200 and isinstance(tok, dict) and tok.get("tokens") else None
    tools = large_tool_array(tool_count_for(need, per))
    body = {"messages": [{"role": "user", "content": "Use the lookup_capital tool to look up the capital of France."}],
            "tools": tools, "tool_choice": "auto", "temperature": 0, "max_tokens": c.get("max_tokens", 4096)}
    st, data = sc.client.post_json("/v1/chat/completions", body, timeout=c.get("timeout", 1800))
    used = (data.get("usage") or {}).get("prompt_tokens", 0) if isinstance(data, dict) else 0
    name, args, raw = first_tool_call(data)
    detail = (f"{len(tools)} tools, {used} prompt tokens: HTTP {st}, tool_calls[0] = "
              f"{name or 'none'}({clip(raw, 60)}) {error_text(data)}").strip()
    if st == 200 and used < need:
        return False, detail + f" (the probe needs at least {need} prompt tokens)"
    ok = st == 200 and name == "lookup_capital" and args is not None and "france" in str(args.get("country", "")).lower()
    return ok, detail


def chk_concurrency(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    n = c.get("parallel", 4)
    prompts = CONCURRENCY_PROMPTS[:n]

    def one(p: tuple[str, str]) -> tuple[int, str]:
        st, data = sc.client.post_json("/completion", {"prompt": p[0], "n_predict": 24, "temperature": 0,
                                                       "cache_prompt": False}, timeout=c.get("timeout", 600))
        return st, (data.get("content", "") if isinstance(data, dict) else "")

    seq = [one(p) for p in prompts]
    with concurrent.futures.ThreadPoolExecutor(n) as ex:
        par = list(ex.map(one, prompts))
    statuses = [s for s, _ in seq + par]
    right = [want.lower() in text.lower() for (_, want), (_, text) in zip(prompts, par)]
    same = sum(a[1] == b[1] for a, b in zip(seq, par))
    ok = all(s == 200 for s in statuses) and all(right) and (same == n or not c.get("require_identical"))
    return ok, (f"{n} simultaneous requests: {sum(s == 200 for s in statuses[n:])}/{n} HTTP 200, "
                f"{sum(right)}/{n} correct, {same}/{n} identical to the sequential answer")


def chk_mtp(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    rec = sc.lane_state.setdefault("mtp", {})
    if c["role"] == "off" and not rec.get("on"):
        raise Skip("the MTP-on session did not run on this lane, nothing to compare")
    st, data = sc.client.post_json("/completion", {"prompt": c.get("prompt", MTP_PROMPT),
                                                   "n_predict": c.get("n_predict", 512), "temperature": 0,
                                                   "cache_prompt": False}, timeout=c.get("timeout", 1200))
    if st != 200 or not isinstance(data, dict):
        return False, f"HTTP {st} {error_text(data)}".strip()
    timings = data.get("timings") or {}
    tps = float(timings.get("predicted_per_second") or 0.0)
    if c["role"] == "on":
        drafted, accepted = draft_stats(timings, sc.logs())
        rec["on"] = {"tps": tps, "text": data.get("content", ""), "drafted": drafted, "accepted": accepted}
        if not drafted:
            return False, f"{tps:.1f} t/s, but no draft tokens were reported: MTP is not active"
        if not accepted:
            return False, f"{tps:.1f} t/s, 0 of {drafted} draft tokens accepted"
        return True, f"{tps:.1f} t/s decode with MTP; draft acceptance {accepted}/{drafted} ({100 * accepted / drafted:.0f}%)"
    on = rec["on"]
    gain = (on["tps"] / tps - 1) * 100 if tps else 0.0
    same = on["text"] == data.get("content", "")
    acc = f"{on['accepted']}/{on['drafted']}" if on.get("drafted") else "n/a"
    return True, (f"decode {on['tps']:.1f} t/s with MTP vs {tps:.1f} t/s without ({gain:+.0f}%); acceptance {acc}; "
                  f"{c.get('n_predict', 512)}-token temp-0 output identical: {'yes' if same else 'no (upstream #25618)'}")


def chk_resident_memory(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    now, source = sc.gate.host.memory()
    taken = sc.preload_available - now
    ok = now >= sc.gate.headroom
    return ok, f"the loaded session took about {gib(taken)}; {gib(now)} available afterwards ({source}, floor {gib(sc.gate.headroom)})"


def chk_log(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    logs = sc.logs()
    problems, seen = [], []
    for pat in c.get("expect", []):
        m = re.search(pat, logs)
        (seen.append(clip(m.group(0), 60)) if m else problems.append(f"log lacks /{pat}/"))
    for pat in c.get("reject", []):
        if re.search(pat, logs):
            problems.append(f"log has /{pat}/")
    for rule in c.get("at_least", []):
        vals = [float(v) for v in re.findall(rule["pattern"], logs)]
        if not vals or max(vals) < rule["min"]:
            problems.append(f"/{rule['pattern']}/ = {max(vals) if vals else 'absent'}, want >= {rule['min']}")
        else:
            seen.append(f"{clip(rule['pattern'], 30)} = {max(vals):g}")
    return not problems, "; ".join(problems) or "found " + ", ".join(seen)


def chk_custom_nodes(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    status = custom_node_status(sc.logs())
    missing = [n for n in c["names"] if n not in status]
    failed = [n for n in c["names"] if status.get(n) is False]
    if missing or failed:
        return False, f"not loaded: {missing}; IMPORT FAILED: {failed}"
    return True, f"imported: {', '.join(c['names'])}"


def chk_speech(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    body = {"input": c["input"], "response_format": "wav"}
    for f in ("voice", "language"):
        if f in c:
            body[f] = c[f]
    st, raw = sc.client.post_raw("/v1/audio/speech", json.dumps(body).encode(), "application/json",
                                 timeout=c.get("timeout", 600))
    if st != 200:
        return False, f"HTTP {st}: {clip(raw.decode(errors='replace'), 120)}"
    seconds, rms = wav_stats(raw)
    ok = seconds >= c.get("min_seconds", 0.5) and rms >= 0.005
    lang = f" ({c['language']})" if "language" in c else ""
    return ok, f"{seconds:.2f} s of audio{lang}, RMS {rms:.3f}" + ("" if ok else " (silent or too short)")


def chk_transcribe(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    data = sc.gate.host.read_bytes(sc.gate.models.path(c["sample"]))
    expected = sc.gate.models.value(c["sample"], "text")
    boundary = "hal0gate" + os.urandom(8).hex()
    body = multipart({"response_format": "json"}, ("file", "sample.wav", data, "audio/wav"), boundary)
    st, raw = sc.client.post_raw("/v1/audio/transcriptions", body, f"multipart/form-data; boundary={boundary}",
                                 timeout=c.get("timeout", 600))
    if st != 200:
        return False, f"HTTP {st}: {clip(raw.decode(errors='replace'), 120)}"
    text = str(json.loads(raw).get("text", ""))
    recall = word_recall(expected, text)
    ok = recall >= c.get("min_recall", 0.8)
    return ok, f"{recall:.0%} of the expected words: {clip(text, 120)!r}"


def chk_stream(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    pcm = pcm16_mono_16k(sc.gate.host.read_bytes(sc.gate.models.path(c["sample"])))
    text = sc.client.stream_partial("/v1/audio/stream", pcm, c.get("timeout", 120))
    return True, f"partial transcript: {clip(text, 120)!r}"


def chk_comfy_generate(sc: SessionCtx, c: dict) -> tuple[bool, str]:
    ckpt = Path(sc.gate.models.path(c["checkpoint"])).name
    w, h, steps = c.get("width", 1024), c.get("height", 1024), c.get("steps", 20)
    st, data = sc.client.post_json("/prompt", {"prompt": comfy_workflow(ckpt, w, h, steps), "client_id": "hal0-gate"})
    pid = data.get("prompt_id") if isinstance(data, dict) else None
    if st != 200 or not pid:
        return False, f"POST /prompt -> HTTP {st}: {clip(json.dumps(data), 200)}"
    deadline = sc.gate.host.now() + c.get("timeout", 1200)
    start = sc.gate.host.now()
    while sc.gate.host.now() < deadline:
        st, raw = sc.client.get(f"/history/{pid}")
        entry = (json.loads(raw) if st == 200 else {}).get(pid) or {}
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            return False, f"the workflow failed: {clip(json.dumps(status.get('messages', [])), 300)}"
        images = [i for out in (entry.get("outputs") or {}).values() for i in out.get("images", [])]
        if images:
            img = images[0]
            q = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                                        "type": img.get("type", "output")})
            st, png = sc.client.get(f"/view?{q}")
            if st != 200:
                return False, f"GET /view -> HTTP {st}"
            size = png_size(png)
            took = sc.gate.host.now() - start
            return size == (w, h), f"{size[0]}x{size[1]} PNG from {ckpt} in {took:.0f} s ({steps} steps)"
        sc.gate.host.sleep(3)
    return False, f"no image after {c.get('timeout', 1200)} s"


CHECKS: dict[str, Callable[[SessionCtx, dict], tuple[bool, str]]] = {
    "http-json": chk_http_json, "completion": chk_completion, "chat": chk_chat, "structured": chk_structured,
    "grammar-size": chk_grammar_size, "tool-call": chk_tool_call, "large-tools": chk_large_tools,
    "concurrency": chk_concurrency, "mtp": chk_mtp, "resident-memory": chk_resident_memory, "log": chk_log,
    "custom-nodes": chk_custom_nodes, "speech": chk_speech, "transcribe": chk_transcribe, "stream": chk_stream,
    "comfy-generate": chk_comfy_generate,
}
assert set(CHECKS) == set(CHECK_KINDS)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_pairs(values: list[str], keys: list[str], what: str, required: bool) -> dict[str, str]:
    """`--digest sha256:..` for one runner, `--digest key=sha256:..` (or comma lists) for a group."""
    toks = [t for v in values for t in re.split(r"[\s,]+", v.strip()) if t]
    out: dict[str, str] = {}
    for t in toks:
        k, sep, v = t.partition("=")
        if not sep:
            if len(keys) != 1:
                raise GateError(f"--{what} {t}: a group needs key={what} for each runner")
            k, v = keys[0], t
        if k not in keys:
            raise GateError(f"--{what} names {k!r}, which is not being gated ({', '.join(keys)})")
        if k in out:
            raise GateError(f"--{what} given twice for {k}")
        out[k] = v
    if required:
        missing = [k for k in keys if k not in out]
        if missing:
            raise GateError(f"--{what} missing for {', '.join(missing)}")
    return out


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


#: The files a report's gate.revision vouches for: the probes, this script and
#: promote's validator. A change to any of them that is not committed would
#: make the report claim a revision whose probes it did not run.
GUARDED_PATHS = ("gates", "scripts", "ci")


def git_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """The caller's environment without any GIT_* variable. GIT_DIR,
    GIT_WORK_TREE, GIT_INDEX_FILE, GIT_OBJECT_DIRECTORY,
    GIT_ALTERNATE_OBJECT_DIRECTORIES, GIT_COMMON_DIR, GIT_CEILING_DIRECTORIES,
    GIT_CONFIG_* (which can set core.worktree) and the like can point git at
    another repository, index or work tree. Then a clean answer about some
    other tree would let a dirty checkout run and record the wrong revision.
    Both checks below only read, so none of those variables is needed."""
    out = {k: v for k, v in (os.environ if env is None else env).items() if not k.startswith("GIT_")}
    out["GIT_OPTIONAL_LOCKS"] = "0"  # git status must not take index.lock
    return out


def git(*args: str) -> list[str]:
    """argv for git on this checkout, whatever the current directory."""
    return ["git", "-C", str(ROOT), *args]


def tree_problems(run: Callable = subprocess.run) -> list[str]:
    """Why this checkout cannot vouch for gate.revision: every changed, staged or
    untracked file under GUARDED_PATHS (ignored files such as __pycache__ do
    not count), or why git could not tell. Empty when the tree is clean."""
    try:
        cp = run(git("status", "--porcelain", "--untracked-files=all", "--", *GUARDED_PATHS),
                 cwd=ROOT, env=git_environment(), capture_output=True, text=True)
    except OSError as e:
        return [f"git is not available: {e}"]
    if cp.returncode != 0:
        return [f"git status failed (not a checkout of this repository?): {clip(cp.stderr or cp.stdout)}"]
    return [line.rstrip() for line in cp.stdout.splitlines() if line.strip()]


def git_revision(run: Callable = subprocess.run) -> str:
    try:
        cp = run(git("rev-parse", "HEAD"), cwd=ROOT, env=git_environment(), capture_output=True, text=True)
    except OSError:
        return ""
    return cp.stdout.strip() if cp.returncode == 0 else ""


def main(argv: list[str] | None = None, host: Host | None = None,
         tree_check: Callable[[], list[str]] = tree_problems) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("key", nargs="?", help="runner key (runners/<key>)")
    ap.add_argument("--group", help="gate every runner of a shared pin, e.g. llama.cpp (standard + cpu)")
    ap.add_argument("--digest", action="append", default=[], help="sha256:<64 hex>, or key=sha256:.. in a group")
    ap.add_argument("--tag", action="append", default=[], help="expected immutable tag (default: the image label)")
    ap.add_argument("--models", help=f"gate-models.toml (default ${MODELS_ENV} or {DEFAULT_MODELS_FILE})")
    ap.add_argument("--models-dir", help="base for relative paths in gate-models.toml (overrides its root)")
    ap.add_argument("--out", help="report file (default gate-<subject>-<tag>-<UTC stamp>.json here)")
    ap.add_argument("--allow-skip", action="append", default=[], metavar="PROBE",
                    help="a probe (or glob, e.g. 'vulkan/*') whose skip does not fail the gate; repeatable")
    ap.add_argument("--headroom-gib", type=float, default=DEFAULT_HEADROOM_GIB,
                    help="refuse a load that leaves less available memory than this (default 8)")
    ap.add_argument("--upload", action="store_true",
                    help="attach the report to the gate-<subject>-<tag> prerelease here and print its URL")
    ap.add_argument("--plan", action="store_true", help="print the probes and podman commands; run nothing")
    ap.add_argument("--podman", default="podman", help="container CLI (default podman)")
    args = ap.parse_args(argv)

    if bool(args.key) == bool(args.group):
        raise GateError("name one runner key or --group")
    keys = group_keys(args.group) if args.group else [args.key]
    if args.key and args.key not in runner.keys():
        raise GateError(f"unknown runner {args.key!r}")
    digests = parse_pairs(args.digest, keys, "digest", required=not args.plan)
    for k, d in digests.items():
        if not DIGEST_RE.match(d):
            raise GateError(f"{k}: {d!r} is not sha256:<64 hex>")
    tags = parse_pairs(args.tag, keys, "tag", required=False)
    models = Models.load(args.models, args.models_dir)
    if args.plan:
        for k in keys:
            print(f"runner {k}: {runner.load(k)['image']['package']}@{digests.get(k, '<digest>')}")
            Gate(k, digests.get(k, "sha256:<digest>"), tags.get(k), models, host or Host(args.podman, False),
                 args.headroom_gib, plan_only=True).run()
        return 0

    problems = tree_check()
    if problems:
        shown = "\n  ".join(problems[:20]) + ("\n  ..." if len(problems) > 20 else "")
        raise GateError("refusing to run: the report records the commit of this checkout as "
                        "gate.revision, but these files under " + ", ".join(f"{p}/" for p in GUARDED_PATHS)
                        + f" differ from it:\n  {shown}\n"
                        "commit or discard them first (--plan runs on a dirty tree)")

    host = host or Host(args.podman)

    def on_signal(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(f"signal {signum}")
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)

    date = utc_now()
    reports: list[dict] = []
    try:
        host.lock()
        for k in keys:
            gate = Gate(k, digests[k], tags.get(k), models, host, args.headroom_gib)
            results = gate.run()
            lanes = list(gate.ver["lanes"])
            reports.append(build_report(key=k, image=gate.image, digest=digests[k], tag=gate.tag or "",
                                        results=results, lanes=lanes, allow_skip=args.allow_skip,
                                        date=date.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                        models=sorted(gate.used_models.values(), key=lambda m: m["key"]),
                                        revision=git_revision()))
    except KeyboardInterrupt:
        print("gate: interrupted; containers removed, no report written", file=sys.stderr)
        return 130
    finally:
        host.cleanup()

    doc = reports[0] if len(reports) == 1 else group_report(reports, reports[0]["date"])
    secrets = models.host_paths() + [str(Path.home()), os.getcwd(), tempfile.gettempdir() + "/hal0-gate-"]
    names = [socket.gethostname(), socket.getfqdn(), socket.gethostname().split(".")[0]]
    doc = scrub(doc, secrets, names)

    subject = args.group or keys[0]
    tag = reports[0]["tag"] if len(reports) == 1 else runner.load(keys[0])["image"]["version"]
    stamp = date.strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out or f"gate-{subject}-{tag}-{stamp}.json")
    write_new(out, json.dumps(doc, indent=2) + "\n")
    self_check(doc, digests)
    for r in reports:
        verdict = "PASSED" if r["passed"] else "FAILED"
        bad = [p["name"] for p in r["probes"] if not p["passed"]]
        print(f"{r['runner']} {r['tag']}: {verdict}" + (f" (failed: {', '.join(bad)})" if bad else "")
              + (f"; skips allowed: {', '.join(s['name'] for s in r['skipped'])}" if r["skipped"] else ""))
    print(f"report: {out}")
    if args.upload:
        if not doc["passed"]:
            print("gate: not uploading a failed report (promote would refuse it)", file=sys.stderr)
            return 1
        rel = release_tag(subject, tag)
        url = upload(out, rel, f"Gate: {subject} {tag}")
        self_check(fetch_public(url), digests)
        print(f"gate_reports URL (verified, readable without credentials): {url}")
    return 0 if doc["passed"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GateError as e:
        print(e.msg, file=sys.stderr)
        sys.exit(2)
