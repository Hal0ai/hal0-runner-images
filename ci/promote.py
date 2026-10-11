#!/usr/bin/env python3
"""Promote a gated runner image digest: verify it, then write the pins.

    promote.py verify  --runner K --digest sha256:.. --gate-report URL --out result.json
    promote.py catalogue --result result.json [--file images.json]
    promote.py hal0-pin  --result result.json --hal0-dir app/ [--create-missing]

`verify` refuses anything that is not (a) an existing image in this repo's
package, (b) built by .github/workflows/build.yml on refs/heads/main (checked
through the build's signed provenance attestation, not through the tag), (c)
reachable under its immutable tag at that same digest, and (d) backed by a gate
report that names the same digest and passed (when the report is fetchable
JSON; otherwise the PR says the report was not machine-checked).

`catalogue` edits only tag/digest of one runner in images.json.
`hal0-pin` edits only one row of the runner pin table in Hal0ai/hal0.

Nothing here merges anything. The workflow opens PRs; a person merges them.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

# ---------------------------------------------------------------------------
# THE ONE MAPPING TO ADJUST when hal0's runner-pin table lands.
#
# hal0 is moving every runner pin into a single table in its manifest.json:
#     key -> {image, tag, digest}
# Everything promote writes into Hal0ai/hal0 is derived from this block and
# from hal0_row() below. Nothing else in this file knows hal0's field names.
# ---------------------------------------------------------------------------
HAL0_PIN_FILE = "manifest.json"          # the ONLY file the hal0 PR may touch
HAL0_PIN_TABLE = "runner_images"         # top-level object in that file (key -> row)  [PLACEHOLDER: confirm against the hal0 PR]
HAL0_FIELDS = {                          # our value -> hal0 row field
    "image": "image",                    # ghcr.io/hal0ai/hal0-runner-<key>
    "tag": "tag",                        # <upstream-version>-r<recipe-sha7>
    "digest": "digest",                  # sha256:... of the GATED image
}
HAL0_TAG_IS_FULL_REF = False             # True if hal0's `tag` holds "<image>:<tag>" (the old toolbox_images style)

# ---------------------------------------------------------------------------
# images.json (this repo). Entry lookup is tolerant of the v1 ("images"/"id")
# and v2 ("runners"/"key") spellings; only tag and digest are ever written.
# ---------------------------------------------------------------------------
CATALOGUE_FILE = "images.json"
CATALOGUE_LIST_KEYS = ("runners", "images")
CATALOGUE_ID_KEYS = ("key", "id")

SIGNER_WORKFLOW = f"{lib.REPO_SLUG}/.github/workflows/build.yml"
SOURCE_REF = "refs/heads/main"
GATE_SCHEMA = "hal0.gate-report.v1"
TAG_LABEL = "org.opencontainers.image.version"


def hal0_row(result: dict) -> dict:
    """The fields promote sets on the hal0 row, under hal0's field names."""
    values = {
        "image": result["image"],
        "tag": f"{result['image']}:{result['tag']}" if HAL0_TAG_IS_FULL_REF else result["tag"],
        "digest": result["digest"],
    }
    return {HAL0_FIELDS[k]: v for k, v in values.items()}


# ---- JSON file editing (pure, tested) --------------------------------------

def dump_like(doc: object, raw: str) -> str:
    """Serialise `doc` the way `raw` was written, so the PR diff is only the pin."""
    original = json.loads(raw)
    for ensure_ascii in (False, True):
        for indent in (2, 4):
            if json.dumps(original, indent=indent, ensure_ascii=ensure_ascii) + "\n" == raw:
                return json.dumps(doc, indent=indent, ensure_ascii=ensure_ascii) + "\n"
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def edit_catalogue(raw: str, result: dict) -> str:
    doc = json.loads(raw)
    for list_key in CATALOGUE_LIST_KEYS:
        entries = doc.get(list_key)
        if isinstance(entries, list):
            break
    else:
        raise lib.CIError(f"{CATALOGUE_FILE}: no {'/'.join(CATALOGUE_LIST_KEYS)} list")
    matches = [e for e in entries if any(e.get(k) == result["runner"] for k in CATALOGUE_ID_KEYS)]
    if len(matches) != 1:
        raise lib.CIError(f"{CATALOGUE_FILE}: expected exactly one entry for {result['runner']!r}, found {len(matches)}")
    entry = matches[0]
    if entry.get("image") and entry["image"] != result["image"]:
        raise lib.CIError(f"{CATALOGUE_FILE}: entry image {entry['image']!r} != {result['image']!r}")
    entry["tag"] = result["tag"]
    entry["digest"] = result["digest"]
    return dump_like(doc, raw)


def edit_hal0_pins(raw: str, result: dict, create_missing: bool = False) -> str:
    doc = json.loads(raw)
    table = doc.get(HAL0_PIN_TABLE)
    if not isinstance(table, dict):
        raise lib.CIError(f"hal0 {HAL0_PIN_FILE}: no object named {HAL0_PIN_TABLE!r}; "
                          "adjust HAL0_PIN_TABLE in ci/promote.py to the table hal0 actually has")
    key = result["runner"]
    if key not in table:
        if not create_missing:
            raise lib.CIError(f"hal0 {HAL0_PIN_FILE}[{HAL0_PIN_TABLE}] has no row for {key!r}; "
                              "hal0 must add the runner first (or re-run with create_missing)")
        table[key] = {}
    table[key].update(hal0_row(result))
    return dump_like(doc, raw)


# ---- verification (network + docker + gh) ----------------------------------

def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def inspect_image(ref: str) -> dict:
    proc = run(["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .}}"])
    if proc.returncode != 0:
        raise lib.CIError(f"{ref} cannot be inspected: {proc.stderr.strip() or proc.stdout.strip()}")
    return json.loads(proc.stdout)


def digest_of(info: dict) -> str:
    return info["manifest"]["digest"]


def labels_of(info: dict) -> dict:
    img = info.get("image") or {}
    if "config" not in img and len(img) == 1:  # per-platform map
        img = next(iter(img.values()))
    return (img.get("config") or {}).get("Labels") or {}


def verify_attestation(image: str, digest: str) -> dict:
    """Signed provenance: built by build.yml in this repo, from refs/heads/main."""
    proc = run([
        "gh", "attestation", "verify", f"oci://{image}@{digest}",
        "--repo", lib.REPO_SLUG,
        "--signer-workflow", SIGNER_WORKFLOW,
        "--source-ref", SOURCE_REF,
        "--format", "json",
    ])
    if proc.returncode != 0:
        raise lib.CIError(
            f"no valid provenance for {image}@{digest} from {SIGNER_WORKFLOW} on {SOURCE_REF}:\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    commit = ""
    try:
        for att in json.loads(proc.stdout):
            cert = att["verificationResult"]["signature"]["certificate"]
            commit = cert.get("sourceRepositoryDigest", "") or commit
    except (ValueError, KeyError, TypeError):
        pass
    return {"source_commit": commit}


def check_gate_report(url: str, runner: str, digest: str, token: str | None) -> tuple[bool, str]:
    """(machine_verified, summary). Raises on a report that fetches and fails."""
    if not url.startswith("https://"):
        raise lib.CIError("gate_report must be an https URL")
    req = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "hal0-runner-images-ci",
        **({"Authorization": f"Bearer {token}"} if token and "github" in url else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read(2_000_000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        raise lib.CIError(f"gate report {url} could not be fetched: {e}")
    try:
        report = json.loads(body)
    except ValueError:
        return False, "report is not machine-readable JSON; a reviewer must read it"
    return True, validate_gate_report(report, runner, digest)


def validate_gate_report(report: dict, runner: str, digest: str) -> str:
    if not isinstance(report, dict) or report.get("schema") != GATE_SCHEMA:
        raise lib.CIError(f"gate report schema is not {GATE_SCHEMA!r}")
    if report.get("runner") != runner or report.get("digest") != digest:
        raise lib.CIError(
            f"gate report is for {report.get('runner')!r}@{report.get('digest')!r}, not {runner!r}@{digest!r}"
        )
    failed = [p.get("name", "?") for p in report.get("probes", []) if p.get("passed") is not True]
    if report.get("passed") is not True or failed:
        raise lib.CIError(f"gate report did not pass (failed probes: {failed or 'overall verdict'})")
    lanes = report.get("lanes") or {}
    passed = sorted(k for k, v in lanes.items() if isinstance(v, dict) and v.get("passed") is True)
    return f"passed; lanes: {', '.join(passed) or 'none reported'}"


def cmd_verify(args: argparse.Namespace) -> int:
    key, digest = args.runner, args.digest
    lib.load_manifest(key)  # the runner must exist in this repo
    if not lib.DIGEST_RE.match(digest):
        raise lib.CIError(f"digest {digest!r} is not sha256:<64 hex>")
    image = lib.package_for(key)

    by_digest = inspect_image(f"{image}@{digest}")
    if digest_of(by_digest) != digest:
        raise lib.CIError(f"{image}@{digest} resolved to a different digest")
    tag = labels_of(by_digest).get(TAG_LABEL, "")
    if not lib.TAG_RE.match(tag):
        raise lib.CIError(f"image label {TAG_LABEL}={tag!r} is not an immutable <version>-r<sha7> tag")
    by_tag = inspect_image(f"{image}:{tag}")
    if digest_of(by_tag) != digest:
        raise lib.CIError(f"{image}:{tag} points at {digest_of(by_tag)}, not {digest}: the tag moved")

    prov = verify_attestation(image, digest)
    verified, summary = check_gate_report(args.gate_report, key, digest, args.token)

    result = {
        "runner": key, "image": image, "tag": tag, "digest": digest,
        "source_commit": prov["source_commit"],
        "gate_report": args.gate_report, "gate_machine_verified": verified, "gate_summary": summary,
    }
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    for k in ("runner", "tag", "digest"):
        lib.set_output(k, result[k])
    return 0


def _load_result(path: str) -> dict:
    result = json.loads(Path(path).read_text())
    for k in ("runner", "image", "tag", "digest"):
        if not result.get(k):
            raise lib.CIError(f"result file lacks {k!r}")
    if not lib.TAG_RE.match(result["tag"]) or not lib.DIGEST_RE.match(result["digest"]):
        raise lib.CIError("result file has a malformed tag or digest")
    return result


def cmd_catalogue(args: argparse.Namespace) -> int:
    path = Path(args.file)
    path.write_text(edit_catalogue(path.read_text(), _load_result(args.result)))
    return 0


def cmd_hal0_pin(args: argparse.Namespace) -> int:
    path = Path(args.hal0_dir) / HAL0_PIN_FILE
    path.write_text(edit_hal0_pins(path.read_text(), _load_result(args.result), args.create_missing))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify")
    v.add_argument("--runner", required=True)
    v.add_argument("--digest", required=True)
    v.add_argument("--gate-report", required=True)
    v.add_argument("--out", required=True)
    v.add_argument("--token", default=None)
    v.set_defaults(func=cmd_verify)

    c = sub.add_parser("catalogue")
    c.add_argument("--result", required=True)
    c.add_argument("--file", default=str(lib.ROOT / CATALOGUE_FILE))
    c.set_defaults(func=cmd_catalogue)

    h = sub.add_parser("hal0-pin")
    h.add_argument("--result", required=True)
    h.add_argument("--hal0-dir", required=True)
    h.add_argument("--create-missing", action="store_true")
    h.set_defaults(func=cmd_hal0_pin)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
