#!/usr/bin/env python3
"""Promote gated runner image digests: verify them, then write the pins.

    promote.py verify    --promotions "standard=sha256:..,cpu=sha256:..,cuda=<tag>@sha256:.."
                         --gate-reports URL | "key=URL,key=URL"  --out result.json
    promote.py pr-body   --result result.json
    promote.py catalogue --result result.json [--file images.json]
    promote.py hal0-pin  --result result.json --hal0-dir app/ [--create-missing]

One run promotes one runner, or a group (the llama.cpp group: standard + cpu +
cuda, which lags with the newest server-cuda-b<M> that exists) into ONE
images.json change and ONE hal0 change.

`verify` refuses any item that is not (a) an existing image in its package
(an external runner such as cuda, which has no recipe here, is checked for
existence and tag only, is written `key=<tag>@<digest>`, and may come without a
gate report: there is no hardware gate for it, and its lanes stay empty), (b) built by
.github/workflows/build.yml on refs/heads/main (checked through the build's
signed provenance attestation, not through the tag), (c) reachable under its
immutable tag at that same digest, and (d) backed by a gate report that is
valid hal0.gate-report.v1 JSON, names the same digest and passed. Anything else,
including a report that is not JSON, fails the whole run: nothing is promoted
unless every item verifies. standard and cpu must name one llama.cpp release and
cuda may not be ahead of it.

A gate report is either one runner's document or a group document
({"schema": ..., "runners": [<per-runner documents>]}); one URL may cover every
item, or each item may name its own (`key=URL`).

`catalogue` edits only tag, digest, candidate, gate_report and lanes_gated of
each promoted runner in images.json. `hal0-pin` edits only those runners' rows
(image, tag, digest, lanes_gated) of the pin table in Hal0ai/hal0.

Nothing here merges anything. The workflow opens PRs; a person merges them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402
from hal0_pins import HAL0_FIELDS, HAL0_PIN_FILE, HAL0_PIN_TABLE, hal0_row  # noqa: E402,F401  (hal0 field names live in hal0_pins.py)

# ---------------------------------------------------------------------------
# images.json (this repo), schema hal0.runner-images.v2: a `runners` array of
# entries keyed by `id` (`key` is accepted too). promote always writes tag and
# digest. It writes candidate (cleared), gate_report and lanes_gated only on an
# entry that already has those fields, so it never invents a field the schema
# check may not know. Nothing else is touched.
# ---------------------------------------------------------------------------
CATALOGUE_FILE = "images.json"
CATALOGUE_LIST_KEY = "runners"
CATALOGUE_ID_KEYS = ("id", "key")

SIGNER_WORKFLOW = f"{lib.REPO_SLUG}/.github/workflows/build.yml"
SOURCE_REF = "refs/heads/main"
GATE_SCHEMA = "hal0.gate-report.v1"
TAG_LABEL = "org.opencontainers.image.version"


# ---- JSON file editing (pure, tested) --------------------------------------

def catalogue_entries(doc: dict) -> list:
    entries = doc.get(CATALOGUE_LIST_KEY)
    if not isinstance(entries, list):
        raise lib.CIError(f"{CATALOGUE_FILE}: no {CATALOGUE_LIST_KEY!r} list")
    return entries


def catalogue_entry(doc: dict, key: str) -> dict:
    matches = [e for e in catalogue_entries(doc) if any(e.get(k) == key for k in CATALOGUE_ID_KEYS)]
    if len(matches) != 1:
        raise lib.CIError(f"{CATALOGUE_FILE}: expected exactly one entry for {key!r}, found {len(matches)}")
    return matches[0]


def edit_catalogue(raw: str, items: list[dict]) -> str:
    doc = json.loads(raw)
    for item in items:
        entry = catalogue_entry(doc, item["runner"])
        if entry.get("image") and entry["image"] != item["image"]:
            raise lib.CIError(f"{CATALOGUE_FILE}: entry image {entry['image']!r} != {item['image']!r}")
        extra = sorted(set(item["lanes_gated"]) - set(entry.get("backends") or item["lanes_gated"]))
        if extra:
            raise lib.CIError(f"{item['runner']}: gate report passed lanes {extra} that the runner does not list in backends")
        entry["tag"] = item["tag"]
        entry["digest"] = item["digest"]
        for field, value in (("candidate", None), ("gate_report", item["gate_report"]),
                             ("lanes_gated", item["lanes_gated"])):  # lanes are admitted by evidence
            if field in entry:
                entry[field] = value
    return lib.dump_like(doc, raw)


def edit_hal0_pins(raw: str, items: list[dict], create_missing: bool = False) -> str:
    doc = json.loads(raw)
    table = doc.get(HAL0_PIN_TABLE)
    if not isinstance(table, dict):
        raise lib.CIError(f"hal0 {HAL0_PIN_FILE}: no object named {HAL0_PIN_TABLE!r}; "
                          "adjust ci/hal0_pins.py to the table hal0 actually has")
    for item in items:
        key = item["runner"]
        if key not in table:
            if not create_missing:
                raise lib.CIError(f"hal0 {HAL0_PIN_FILE}[{HAL0_PIN_TABLE}] has no row for {key!r}; "
                                  "hal0 must add the runner first (or re-run with create_missing)")
            table[key] = {}
        table[key].update(hal0_row(item))
    return lib.dump_like(doc, raw)


# ---- the group (pure, tested) ----------------------------------------------

KEY_ITEM = re.compile(r"^([a-z0-9][a-z0-9-]*)=(.+)$")
LLAMA_RELEASE = re.compile(r"(?<![A-Za-z0-9])b(\d+)(?!\d)")


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s,]+", text.strip()) if t]


def parse_promotions(text: str) -> list[dict]:
    """`key=sha256:..` (built runner) or `key=<tag>@sha256:..` (external runner)."""
    items, seen = [], set()
    for tok in _tokens(text):
        m = KEY_ITEM.match(tok)
        if not m:
            raise lib.CIError(f"promotion {tok!r}: expected key=<digest> or key=<tag>@<digest>")
        key, rest = m.groups()
        tag, _, digest = rest.rpartition("@")
        if not lib.DIGEST_RE.match(digest):
            raise lib.CIError(f"promotion {tok!r}: {digest!r} is not sha256:<64 hex>")
        if key in seen:
            raise lib.CIError(f"promotion lists {key!r} twice")
        seen.add(key)
        items.append({"runner": key, "digest": digest, "tag": tag})
    if not items:
        raise lib.CIError("no promotions given")
    return items


def parse_gate_reports(text: str, keys: list[str], optional: frozenset[str] = frozenset()) -> dict[str, str | None]:
    """key -> report URL. One bare URL covers every key; `key=URL` names one.

    A key in `optional` (an external runner with no hardware gate) may have none.
    """
    per_key: dict[str, str] = {}
    default = ""
    for tok in _tokens(text):
        m = KEY_ITEM.match(tok)
        if m and m.group(2).startswith("https://"):
            per_key[m.group(1)] = m.group(2)
        elif tok.startswith("https://"):
            if default:
                raise lib.CIError("more than one bare gate report URL; name each as key=URL")
            default = tok
        else:
            raise lib.CIError(f"gate report {tok!r}: expected an https URL or key=URL")
    unknown = sorted(set(per_key) - set(keys))
    if unknown:
        raise lib.CIError(f"gate report given for {unknown}, which are not being promoted")
    missing = [k for k in keys if k not in per_key and not default and k not in optional]
    if missing:
        raise lib.CIError(f"no gate report for {missing}")
    return {k: per_key.get(k) or default or None for k in keys}


LLAMA_GROUP = ("standard", "cpu")   # built here, always the same b<N> release
CUDA = "cuda"                         # ggml-org's image: the newest server-cuda-b<M> with M <= N


def llama_release(tag: str) -> int | None:
    m = LLAMA_RELEASE.search(tag)
    return int(m.group(1)) if m else None


def check_group(items: list[dict]) -> int | None:
    """standard and cpu must name one release; cuda may not be ahead of it.

    ggml-org does not publish server-cuda for every release, so cuda is promoted
    with the newest tag that exists (M <= N). Returns N, or None when neither
    standard nor cpu is in the group.
    """
    built = {i["runner"]: llama_release(i["tag"]) for i in items if i["runner"] in LLAMA_GROUP}
    if None in built.values():
        raise lib.CIError(f"cannot read a b<N> release from the tag of {[k for k, v in built.items() if v is None]}")
    if len(set(built.values())) > 1:
        raise lib.CIError(f"standard and cpu must pin the same llama.cpp release, got {built}")
    release = next(iter(built.values()), None)
    for i in items:
        if i["runner"] == CUDA:
            m = llama_release(i["tag"])
            if m is None:
                raise lib.CIError(f"cannot read a b<M> release from the cuda tag {i['tag']!r}")
            if release is not None and m > release:
                raise lib.CIError(f"cuda tag {i['tag']} is ahead of the group's release b{release}")
    return release


def describe(items: list[dict]) -> tuple[str, str]:
    """(branch, pull request title) for a promotion."""
    keys = [i["runner"] for i in items]
    release = check_group(items)
    if len(items) == 1:
        label = f"{keys[0]} to {items[0]['tag']}"
        branch = f"bot/promote-{keys[0]}-{items[0]['tag']}"
    else:
        digests = hashlib.sha256("".join(sorted(i["digest"] for i in items)).encode()).hexdigest()[:7]
        what = f"llama.cpp b{release}" if release and set(keys) <= {*LLAMA_GROUP, CUDA} else f"{len(keys)} runners"
        cuda = next((i["tag"] for i in items if i["runner"] == CUDA), "")
        label = f"{', '.join(keys)} to {what}" + (f" (cuda {cuda})" if cuda and release else "")
        branch = f"bot/promote-{'-'.join(sorted(keys))}-{digests}"
    return branch, f"chore(runners): pin {label}"


def pr_body(items: list[dict]) -> str:
    out = []
    for i in items:
        out.append(f"### `{i['runner']}`: `{i['image']}:{i['tag']}`\n")
        out.append(f"- digest: `{i['digest']}`")
        if i["external"]:
            out.append("- **UNGATED external image**: ggml-org's image, not built here, so there is no build provenance "
                       "to verify and no hardware gate (the CUDA lane is hidden in hal0); `lanes_gated` stays empty. "
                       "Only the registry digest and tag were checked.")
        if i["source_commit"]:
            out.append(f"- built from `{i['source_commit']}` of Hal0ai/hal0-runner-images (signed build provenance, refs/heads/main)")
        if i["gate_report"]:
            out.append(f"- gate report: {i['gate_report']}")
        out.append(f"- gate result: {i['gate_summary']}\n")
    out.append("Promoted by the Promote runner image workflow in Hal0ai/hal0-runner-images "
               "(docs/UPDATING.md). It verified that each digest exists, that the build workflow on main "
               "produced it, that its immutable tag still points at it, and that a passing gate report names it. "
               "It does not merge: a person reviews the pin.")
    return "\n".join(out) + "\n"


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


def fetch_gate_report(url: str, token: str | None) -> object:
    if not url.startswith("https://"):
        raise lib.CIError("gate report must be an https URL")
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
        return json.loads(body)
    except ValueError:
        raise lib.CIError(f"gate report {url} is not JSON; it must be a {GATE_SCHEMA} document")


def report_for(doc: object, runner: str) -> dict:
    """One runner's document: the report itself, or its entry in a group report."""
    if not isinstance(doc, dict) or doc.get("schema") != GATE_SCHEMA:
        raise lib.CIError(f"gate report schema is not {GATE_SCHEMA!r}")
    if isinstance(doc.get("runners"), list):
        mine = [r for r in doc["runners"] if isinstance(r, dict) and r.get("runner") == runner]
        if len(mine) != 1:
            raise lib.CIError(f"group gate report has {len(mine)} entries for {runner!r}, expected 1")
        return {"schema": GATE_SCHEMA, **mine[0]}
    return doc


def validate_gate_report(report: dict, runner: str, digest: str) -> tuple[str, list]:
    """(summary, lanes_gated). Raises unless the report passed for this runner and digest."""
    if not isinstance(report, dict) or report.get("schema") != GATE_SCHEMA:
        raise lib.CIError(f"gate report schema is not {GATE_SCHEMA!r}")
    if report.get("runner") != runner or report.get("digest") != digest:
        raise lib.CIError(
            f"gate report is for {report.get('runner')!r}@{report.get('digest')!r}, not {runner!r}@{digest!r}"
        )
    failed = [p.get("name", "?") for p in report.get("probes", []) if p.get("passed") is not True]
    if report.get("passed") is not True or failed:
        raise lib.CIError(f"gate report for {runner} did not pass (failed probes: {failed or 'overall verdict'})")
    lanes = report.get("lanes") or {}
    passed = sorted(k for k, v in lanes.items() if isinstance(v, dict) and v.get("passed") is True)
    return f"passed; lanes: {', '.join(passed) or 'none reported'}", passed


TAG_CHARS = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


def has_entry(doc: object, runner: str) -> bool:
    if not isinstance(doc, dict):
        return False
    if isinstance(doc.get("runners"), list):
        return any(isinstance(r, dict) and r.get("runner") == runner for r in doc["runners"])
    return doc.get("runner") == runner


def verify_item(item: dict, report_doc: object, report_url: str | None, built_keys: list[str]) -> dict:
    key, digest = item["runner"], item["digest"]
    if not lib.KEY_RE.match(key):
        raise lib.CIError(f"invalid runner key {key!r}")
    # A runner without runners/<key>/ is an external image (cuda): someone
    # else's image listed in the catalogue. It has no build provenance from
    # this repo, so it must come with an explicit tag.
    external = key not in built_keys
    tag = item["tag"]
    if external:
        image = catalogue_entry(json.loads((lib.ROOT / CATALOGUE_FILE).read_text()), key).get("image", "")
        if not image or image.startswith(lib.PACKAGE_PREFIX):
            raise lib.CIError(f"{key!r} has no recipe here and no external image in {CATALOGUE_FILE}")
        if not TAG_CHARS.match(tag):
            raise lib.CIError(f"{key}: an external runner is written {key}=<tag>@<digest>")
    else:
        image = lib.package_for(key)
        if tag:
            raise lib.CIError(f"{key}: a built runner is written {key}=<digest>; its tag comes from its label")

    by_digest = inspect_image(f"{image}@{digest}")
    if digest_of(by_digest) != digest:
        raise lib.CIError(f"{image}@{digest} resolved to a different digest")
    if not external:
        tag = labels_of(by_digest).get(TAG_LABEL, "")
        if not lib.TAG_RE.match(tag):
            raise lib.CIError(f"{key}: image label {TAG_LABEL}={tag!r} is not an immutable <version>-r<sha7> tag")
    by_tag = inspect_image(f"{image}:{tag}")
    if digest_of(by_tag) != digest:
        raise lib.CIError(f"{image}:{tag} points at {digest_of(by_tag)}, not {digest}: the tag moved")

    source_commit = "" if external else verify_attestation(image, digest)["source_commit"]
    if external and (report_url is None or not has_entry(report_doc, key)):
        # No hardware gate for an external image (the CUDA lane is hidden in hal0):
        # its digest and tag were verified above, and nothing is claimed about lanes.
        summary, lanes, report_url = "no hardware gate (external image); registry digest and tag verified", [], None
    else:
        summary, lanes = validate_gate_report(report_for(report_doc, key), key, digest)
    return {
        "runner": key, "image": image, "tag": tag, "digest": digest, "external": external,
        "source_commit": source_commit, "gate_report": report_url, "gate_summary": summary,
        "lanes_gated": lanes,
    }


def cmd_verify(args: argparse.Namespace) -> int:
    items = parse_promotions(args.promotions)
    built = lib.runner_keys()
    urls = parse_gate_reports(args.gate_reports, [i["runner"] for i in items],
                              frozenset(i["runner"] for i in items if i["runner"] not in built))
    docs = {u: fetch_gate_report(u, args.token) for u in set(urls.values()) if u}
    results = [verify_item(i, docs.get(urls[i["runner"]]), urls[i["runner"]], built) for i in items]
    branch, title = describe(results)  # also refuses a mismatched llama.cpp group

    Path(args.out).write_text(json.dumps({"items": results}, indent=2) + "\n")
    print(json.dumps({"items": results}, indent=2))
    lib.set_output("branch", branch)
    lib.set_output("title", title)
    return 0


def _load_result(path: str) -> list[dict]:
    items = json.loads(Path(path).read_text()).get("items") or []
    if not items:
        raise lib.CIError("result file has no items")
    for item in items:
        for k in ("runner", "image", "tag", "digest", "gate_report", "lanes_gated"):
            if k not in item or (item[k] in ("", None) and not (k == "gate_report" and item.get("external"))):
                raise lib.CIError(f"result item lacks {k!r}")
        tag_ok = TAG_CHARS.match(item["tag"]) if item.get("external") else lib.TAG_RE.match(item["tag"])
        if not tag_ok or not lib.DIGEST_RE.match(item["digest"]):
            raise lib.CIError(f"{item['runner']}: malformed tag or digest in the result file")
    return items


def cmd_pr_body(args: argparse.Namespace) -> int:
    sys.stdout.write(pr_body(_load_result(args.result)))
    return 0


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
    v.add_argument("--promotions", required=True)
    v.add_argument("--gate-reports", required=True)
    v.add_argument("--out", required=True)
    v.add_argument("--token", default=None)
    v.set_defaults(func=cmd_verify)

    b = sub.add_parser("pr-body")
    b.add_argument("--result", required=True)
    b.set_defaults(func=cmd_pr_body)

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
