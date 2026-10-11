#!/usr/bin/env python3
"""Build-time checks for the hal0 ComfyUI image. Fails the build on any problem.

    cd /opt/ComfyUI && python /opt/hal0/check_custom_nodes.py /opt/hal0/constraints.txt

1. Every Python package installed in the venv is pinned, at the installed
   version, in constraints.txt. A dependency that slipped in unpinned would
   make the next rebuild resolve something different.
2. Every bundled custom node under custom_nodes/ loads through ComfyUI's own
   loader (nodes.load_custom_node), in CPU mode because the build host has no
   GPU. A node whose dependencies are missing (the ComfyUI-GGUF "No module
   named 'gguf'" defect) fails here instead of at runtime on a box.
3. Runtime imports a node makes only when it runs work: onnxruntime (with
   the CPU provider) and rembg's session factory, for background removal.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
import re
import sys


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check_pins(constraints: str) -> list[str]:
    pinned: dict[str, str] = {}
    for line in open(constraints, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line:
            name, _, version = line.partition("==")
            pinned[canonical(name)] = version.strip()
    problems = []
    seen: set[str] = set()
    for dist in importlib.metadata.distributions():
        name = canonical(dist.metadata["Name"])
        # pip comes with the base image's ensurepip (pinned by the base digest);
        # a venv on Fedora lists each package twice, via lib and lib64.
        if name == "pip" or name in seen:
            continue
        seen.add(name)
        want = pinned.get(name)
        if want is None:
            problems.append(f"{name}=={dist.version} is installed but not pinned in constraints.txt")
        elif want != dist.version:
            problems.append(f"{name} is {dist.version}, constraints.txt pins {want}")
    return sorted(problems)


async def load_nodes() -> list[str]:
    sys.argv = [sys.argv[0], "--cpu"]
    sys.path.insert(0, os.getcwd())
    import comfy.options

    comfy.options.enable_args_parsing()
    import nodes

    base = set(nodes.NODE_CLASS_MAPPINGS)
    failed = []
    root = os.path.join(os.getcwd(), "custom_nodes")
    found = sorted(d for d in os.listdir(root)
                   if os.path.isdir(os.path.join(root, d)) and not d.startswith((".", "__")))
    if not found:
        return ["no custom nodes found under custom_nodes/"]
    for name in found:
        before = len(nodes.NODE_CLASS_MAPPINGS)
        ok = await nodes.load_custom_node(os.path.join(root, name), base)
        added = len(nodes.NODE_CLASS_MAPPINGS) - before
        print(f"custom node {name}: {'loaded' if ok else 'FAILED'}, {added} node class(es)", flush=True)
        if not ok:
            failed.append(f"custom node {name} failed to load (traceback above)")
    return failed


def check_runtimes() -> list[str]:
    """Imports custom nodes only make at run time, so a node can load and still fail later."""
    problems = []
    try:
        import onnxruntime

        providers = onnxruntime.get_available_providers()
        print(f"onnxruntime {onnxruntime.__version__}: providers {providers}", flush=True)
        if "CPUExecutionProvider" not in providers:
            problems.append(f"onnxruntime has no CPUExecutionProvider (providers: {providers})")
        from rembg import new_session  # noqa: F401  (rembg's session factory; builds nothing)
    except Exception as e:  # noqa: BLE001 - report any import failure as a build failure
        problems.append(f"rembg's ONNX runtime is not usable: {e!r}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 64
    problems = check_pins(argv[0])
    problems += asyncio.run(load_nodes())
    problems += check_runtimes()
    for p in problems:
        print(f"check_custom_nodes: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
