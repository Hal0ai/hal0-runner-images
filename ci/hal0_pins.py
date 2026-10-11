"""The ONE mapping to Hal0ai/hal0's runner pin table.

hal0 keeps every runner pin in a single table in its manifest.json:
    key -> {image, tag, digest}
promote.py writes that table; scripts/retention.py reads it to know which images
hal0 still pins. Nothing else in this repository knows hal0's field names, so when
the hal0 PR lands, adjust the constants below and nothing else.

PLACEHOLDER until hal0's shape is confirmed: HAL0_PIN_TABLE and HAL0_FIELDS.
"""

from __future__ import annotations

import json

HAL0_REPO = "Hal0ai/hal0"
HAL0_PIN_FILE = "manifest.json"          # the ONLY file a promote PR in hal0 may touch
HAL0_PIN_TABLE = "runner_images"         # top-level object in that file (key -> row)
HAL0_FIELDS = {                          # our value -> hal0 row field
    "image": "image",                    # ghcr.io/hal0ai/hal0-runner-<key>
    "tag": "tag",                        # <upstream-version>-r<recipe-sha7>
    "digest": "digest",                  # sha256:... of the GATED image
}
HAL0_TAG_IS_FULL_REF = False             # True if hal0's `tag` holds "<image>:<tag>" (the old toolbox_images style)
HAL0_PIN_URL = f"https://raw.githubusercontent.com/{HAL0_REPO}/main/{HAL0_PIN_FILE}"


def hal0_row(result: dict) -> dict:
    """The fields promote sets on the hal0 row, under hal0's field names."""
    values = {
        "image": result["image"],
        "tag": f"{result['image']}:{result['tag']}" if HAL0_TAG_IS_FULL_REF else result["tag"],
        "digest": result["digest"],
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
        if not image:
            raise ValueError(f"row {key!r} has no {HAL0_FIELDS['image']!r}")
        if tag:
            refs.append(tag if HAL0_TAG_IS_FULL_REF else f"{image}:{tag}")
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
