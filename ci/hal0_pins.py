"""The ONE mapping to Hal0ai/hal0's runner pin table.

hal0's repo-root manifest.json (`"_schema": "hal0.manifest.v2"`) has a top-level
`runners` object keyed by runner key (standard, cpu, flm, kokoro, moonshine,
comfyui, qwen3tts, cuda). Each row has exactly four fields:

    image         bare repository, never containing ':' or '@'
                  (ghcr.io/hal0ai/hal0-runner-<key>; cuda: ghcr.io/ggml-org/llama.cpp)
    tag           BARE tag (b11552-r1a2b3c4; cuda: server-cuda-b11552); null = unpublished
    digest        sha256:<64 hex>; hal0 rejects a digest without a tag
    lanes_gated   e.g. ["rocm", "vulkan"]

promote.py writes those four fields for the runner it promotes; scripts/retention.py
reads the table to know which images hal0 still pins. Nothing else in this
repository knows hal0's field names, so if the shape changes, change it here.
(hal0 enforces the rest: hal0.runners.pins, and `--check-release` in its release gate.)
"""

from __future__ import annotations

import json

HAL0_REPO = "Hal0ai/hal0"
HAL0_PIN_FILE = "manifest.json"          # the ONLY file a promote PR in hal0 may touch
HAL0_PIN_TABLE = "runners"               # top-level object in that file (key -> row)
HAL0_FIELDS = {                          # our value -> hal0 row field
    "image": "image",                    # ghcr.io/hal0ai/hal0-runner-<key>
    "tag": "tag",                        # <upstream-version>-r<recipe-sha7>
    "digest": "digest",                  # sha256:... of the GATED image
    "lanes_gated": "lanes_gated",        # lanes whose gate passed, from the gate report
}
HAL0_PIN_URL = f"https://raw.githubusercontent.com/{HAL0_REPO}/main/{HAL0_PIN_FILE}"


def hal0_row(result: dict) -> dict:
    """The fields promote sets on the hal0 row, under hal0's field names."""
    values = {
        "image": result["image"],
        "tag": result["tag"],
        "digest": result["digest"],
        "lanes_gated": list(result["lanes_gated"]),
    }
    return {HAL0_FIELDS[k]: v for k, v in values.items()}


def table_of(doc: object) -> dict | None:
    table = doc.get(HAL0_PIN_TABLE) if isinstance(doc, dict) else None
    return table if isinstance(table, dict) else None


def pin_refs(table: dict) -> list[str]:
    """Image refs hal0 pins, as retention understands them: repo:tag and repo@digest."""
    refs: list[str] = []
    for key, row in table.items():
        if not isinstance(row, dict):
            raise ValueError(f"row {key!r} is not an object")
        image = row.get(HAL0_FIELDS["image"])
        tag = row.get(HAL0_FIELDS["tag"])
        digest = row.get(HAL0_FIELDS["digest"])
        if not image or ":" in image or "@" in image:
            raise ValueError(f"row {key!r}: {HAL0_FIELDS['image']!r} must be a bare repository, got {image!r}")
        if tag:
            refs.append(f"{image}:{tag}")
        if digest:
            refs.append(f"{image}@{digest}")
    return refs


def parse_pin_table(raw: str, url: str) -> list[str]:
    """Parse hal0's manifest.json into pinned image refs. Fails loud (SystemExit)."""
    try:
        doc = json.loads(raw)
    except ValueError as e:
        raise SystemExit(f"FATAL: hal0 pin file at {url} is not valid JSON: {e}")
    table = table_of(doc)
    if table is None:
        raise SystemExit(
            f"FATAL: hal0 pin file at {url} has no object named {HAL0_PIN_TABLE!r}; "
            "adjust ci/hal0_pins.py to the table hal0 actually has"
        )
    try:
        refs = pin_refs(table)
    except ValueError as e:
        raise SystemExit(f"FATAL: hal0 pin file at {url}: {e}")
    if not refs:
        raise SystemExit(
            f"FATAL: hal0 pin file at {url} lists ZERO pinned images. hal0 always pins at least "
            "one runner, so this means the table is broken or empty. Refusing to run against a "
            "suspect pin list."
        )
    return refs
