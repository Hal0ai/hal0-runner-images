#!/usr/bin/env python3
"""GHCR retention sweep for the hal0ai org's runner-image packages.

Implements hal0 runner-images v3 spec §9. Deletes GHCR container-image
VERSIONS (not tags, not repos) that look like disposable CI/cosign debris,
while keeping everything images.json or retention-allowlist.json names,
every mutable-pointer tag, the newest N release-shaped tags per package,
everything younger than a grace window, and — always — every UNTAGGED
version (it may be a platform child of a kept multi-arch index; GHCR does
not expose that parent/child link over this API, so untagged is a hard
keep, no exceptions).

Fail-safe by construction: anything the policy can't positively identify
as CI/cosign debris is reported "unclassified — kept by default" and left
alone. This is intentional in v1 — plain release-shaped tags beyond the
newest-N are NOT deleted, only reported. Automated deletion of aged
releases is a follow-up once we have more confidence in the classifier.

stdlib only (urllib/json/argparse/datetime/re/os/sys) — no pip installs
in this workflow, on purpose (see pin-digests.yml's BUILD-FREE philosophy:
keep the fast/frequent ops dependency-free).

Usage:
    GH_TOKEN=... python3 scripts/retention.py                  # dry-run (default)
    GH_TOKEN=... python3 scripts/retention.py --delete          # actually delete
    python3 scripts/retention.py --org hal0ai --keep-releases 4 --grace-days 14

The classification core (classify_package / classify_org) takes plain
dicts and does no I/O, so it's unit-testable without hitting the GitHub
API — see scripts/test_retention_fixtures.py.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Constants / shapes
# ---------------------------------------------------------------------------

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"

# Tags that point at a moving target rather than an immutable build — never
# eligible for deletion regardless of age.
MUTABLE_POINTER_TAGS = {"latest", "main", "master", "edge", "nightly", "server"}

# Per-commit CI tag, e.g. "sha-a1b2c3d" or a full 40-char sha.
RE_SHA_CI_TAG = re.compile(r"^sha-[0-9a-f]{7,40}$")

# Cosign signature/attestation artifact tag, e.g.
# "sha256-<64 hex>.sig" or "sha256-<64 hex>.att" (untagged suffix variants
# also occur but are rare; we require the standard cosign shape here).
RE_COSIGN_TAG = re.compile(r"^sha256-([0-9a-f]{64})(\.sig|\.att)?$")

# Release-shaped tag: all-numeric ("12", "2026") or "v" + dotted-numeric
# ("v1.2.3", "v1.2").
RE_RELEASE_TAG = re.compile(r"^(\d+|v\d+(\.\d+)*)$")

DEFAULT_ORG = "hal0ai"
DEFAULT_KEEP_RELEASES = 4
DEFAULT_GRACE_DAYS = 14


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def is_sha_ci_tag(tag: str) -> bool:
    return bool(RE_SHA_CI_TAG.match(tag))


def is_cosign_tag(tag: str) -> bool:
    return bool(RE_COSIGN_TAG.match(tag))


def is_release_tag(tag: str) -> bool:
    return bool(RE_RELEASE_TAG.match(tag))


def is_mutable_pointer_tag(tag: str) -> bool:
    return tag in MUTABLE_POINTER_TAGS


def cosign_subject_digest(tag: str) -> str | None:
    """Given a cosign artifact tag, return the sha256 digest it signs/attests."""
    m = RE_COSIGN_TAG.match(tag)
    if not m:
        return None
    return f"sha256:{m.group(1)}"


def parse_github_datetime(s: str) -> datetime:
    # GitHub timestamps look like "2026-07-19T12:34:56Z".
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def package_name_from_ref(image: str) -> str:
    """'ghcr.io/hal0ai/hal0-toolbox-cpu' -> 'hal0-toolbox-cpu'."""
    return image.rstrip("/").split("/")[-1]


# ---------------------------------------------------------------------------
# Index builders (pure — take already-loaded JSON dicts)
# ---------------------------------------------------------------------------


def build_images_index(images_doc: dict) -> dict:
    """package name -> {"tags": set(...), "digests": set(...)} from images.json."""
    index: dict[str, dict[str, set]] = {}
    for entry in images_doc.get("images", []):
        image = entry.get("image")
        if not image:
            continue
        pkg = package_name_from_ref(image)
        slot = index.setdefault(pkg, {"tags": set(), "digests": set()})
        if entry.get("tag"):
            slot["tags"].add(entry["tag"])
        if entry.get("digest"):
            slot["digests"].add(entry["digest"])
    return index


def _refs_to_index(refs: list, index: dict) -> None:
    for ref in refs:
        if ":" not in ref:
            continue
        repo_part, tag = ref.rsplit(":", 1)
        pkg = package_name_from_ref(repo_part)
        index.setdefault(pkg, set()).add(tag)


def build_allowlist_index(allowlist_doc: dict) -> dict:
    """package name -> set(tags) from retention-allowlist.json."""
    index: dict[str, set] = {}
    _refs_to_index(allowlist_doc.get("hal0_code_pins", []), index)
    _refs_to_index(allowlist_doc.get("evidence", {}).get("refs", []), index)
    return index


# ---------------------------------------------------------------------------
# Classification core — pure, no I/O. This is what the fixture tests exercise.
# ---------------------------------------------------------------------------


def classify_package(
    pkg_name: str,
    versions: list,
    images_index: dict,
    allowlist_index: dict,
    *,
    keep_releases: int = DEFAULT_KEEP_RELEASES,
    grace_days: int = DEFAULT_GRACE_DAYS,
    now: datetime | None = None,
) -> dict:
    """Classify every version of one package.

    `versions` is a list of dicts shaped like the GitHub API's container
    version objects, but pre-flattened by the caller to at least:
        {"id": int, "digest": "sha256:...", "created_at": datetime, "tags": [str, ...]}

    Returns {version_id: {"status": "keep"|"delete"|"unclassified",
                           "reasons": [str, ...], "tags": [...], "created_at": datetime}}
    """
    now = now or datetime.now(timezone.utc)
    grace_cutoff = now - timedelta(days=grace_days)

    img_tags = images_index.get(pkg_name, {}).get("tags", set())
    img_digests = images_index.get(pkg_name, {}).get("digests", set())
    allow_tags = allowlist_index.get(pkg_name, set())

    digest_to_version = {v["digest"]: v for v in versions}

    # Rule (d): newest keep_releases versions that carry a release-shaped tag.
    release_versions = [v for v in versions if any(is_release_tag(t) for t in v["tags"])]
    release_versions_sorted = sorted(release_versions, key=lambda v: v["created_at"], reverse=True)
    kept_release_ids = {v["id"] for v in release_versions_sorted[:keep_releases]}

    results: dict[int, dict] = {}

    # Pass 1: evaluate KEEP rules (a)-(f) for every version.
    for v in versions:
        tags = v["tags"]
        reasons = []

        if v["digest"] in img_digests:
            reasons.append("images.json: pinned digest")
        matched_img_tags = [t for t in tags if t in img_tags]
        if matched_img_tags:
            reasons.append(f"images.json: tag {matched_img_tags}")

        matched_allow_tags = [t for t in tags if t in allow_tags]
        if matched_allow_tags:
            reasons.append(f"retention-allowlist.json: tag {matched_allow_tags}")

        matched_mutable = [t for t in tags if is_mutable_pointer_tag(t)]
        if matched_mutable:
            reasons.append(f"mutable pointer tag {matched_mutable}")

        if v["id"] in kept_release_ids:
            reasons.append(f"newest {keep_releases} release-shaped tags for this package")

        if not tags:
            reasons.append("untagged — never deleted (possible multi-arch index child)")

        if v["created_at"] > grace_cutoff:
            age_days = (now - v["created_at"]).days
            reasons.append(f"within {grace_days}d grace window (age {age_days}d)")

        if reasons:
            results[v["id"]] = {
                "status": "keep",
                "reasons": reasons,
                "tags": tags,
                "created_at": v["created_at"],
            }
        else:
            results[v["id"]] = {
                "status": "pending",
                "reasons": [],
                "tags": tags,
                "created_at": v["created_at"],
            }

    # Pass 2a: resolve pending versions that carry NO cosign tags. These are
    # either pure per-commit CI sha tags (-> delete) or something the policy
    # doesn't name (-> unclassified, kept by default).
    for v in versions:
        r = results[v["id"]]
        if r["status"] != "pending":
            continue
        tags = v["tags"]
        cosign_tags = [t for t in tags if is_cosign_tag(t)]
        if cosign_tags:
            continue  # handled in pass 2b
        if tags and all(is_sha_ci_tag(t) for t in tags):
            r["status"] = "delete"
            r["reasons"] = ["per-commit CI sha tag(s), no keep rule matched"]
        else:
            r["status"] = "unclassified"
            r["reasons"] = [
                "no keep rule matched, and tag shape isn't recognized CI/cosign "
                "debris — kept by default (fail-safe). Note: release-shaped tags "
                "beyond the newest-N are deliberately left here in v1; only "
                "CI/cosign debris is auto-deleted."
            ]

    # Pass 2b: resolve pending versions that DO carry cosign tags. Their fate
    # depends on the subject version's (now-resolved) status.
    for v in versions:
        r = results[v["id"]]
        if r["status"] != "pending":
            continue
        tags = v["tags"]
        if not all(is_sha_ci_tag(t) or is_cosign_tag(t) for t in tags):
            r["status"] = "unclassified"
            r["reasons"] = [
                "no keep rule matched, and tag shape isn't recognized CI/cosign "
                "debris — kept by default (fail-safe)."
            ]
            continue

        cosign_tags = [t for t in tags if is_cosign_tag(t)]
        subject_notes = []
        any_subject_kept = False
        for t in cosign_tags:
            subj_digest = cosign_subject_digest(t)
            subj_version = digest_to_version.get(subj_digest)
            if subj_version is None:
                subject_notes.append(f"{t}: subject {subj_digest} absent from package -> eligible")
                continue
            subj_status = results[subj_version["id"]]["status"]
            if subj_status == "keep":
                any_subject_kept = True
                subject_notes.append(f"{t}: subject {subj_digest} is KEPT -> cosign artifact kept too")
            else:
                subject_notes.append(f"{t}: subject {subj_digest} status={subj_status} -> eligible")

        if any_subject_kept:
            r["status"] = "keep"
            r["reasons"] = ["cosign artifact's subject is kept"] + subject_notes
        else:
            r["status"] = "delete"
            r["reasons"] = ["cosign artifact; subject is deleted or absent"] + subject_notes

    return results


def classify_org(
    packages: dict,
    images_index: dict,
    allowlist_index: dict,
    *,
    keep_releases: int = DEFAULT_KEEP_RELEASES,
    grace_days: int = DEFAULT_GRACE_DAYS,
    now: datetime | None = None,
) -> dict:
    """packages: {pkg_name: [version dicts]} -> {pkg_name: classify_package(...)}."""
    return {
        pkg_name: classify_package(
            pkg_name,
            versions,
            images_index,
            allowlist_index,
            keep_releases=keep_releases,
            grace_days=grace_days,
            now=now,
        )
        for pkg_name, versions in packages.items()
    }


# ---------------------------------------------------------------------------
# Reporting — pure, given the classification result.
# ---------------------------------------------------------------------------


def render_report(org: str, all_results: dict, *, dry_run: bool, keep_releases: int, grace_days: int) -> str:
    lines = []
    lines.append("# GHCR retention report")
    lines.append("")
    lines.append(f"Org: `{org}` | mode: {'DRY-RUN (no deletions performed)' if dry_run else 'DELETE'} | "
                  f"keep-releases: {keep_releases} | grace-days: {grace_days}")
    lines.append("")
    lines.append(
        "Policy notes: untagged versions are NEVER deleted (rule e — a version "
        "with no tags may be a platform child of a kept multi-arch index; GHCR's "
        "API does not expose that parent/child link, so untagged is a hard keep "
        "with no exceptions). Plain numeric/`vX.Y.Z` release tags beyond the "
        "newest-N are reported as `unclassified — kept by default`, not deleted: "
        "v1 only auto-deletes CI (`sha-<hex>`) and cosign (`sha256-<hex>.sig`/`.att`) "
        "debris; releases are never auto-deleted."
    )
    lines.append("")

    total_keep = total_delete = total_unclassified = 0

    for pkg_name in sorted(all_results):
        results = all_results[pkg_name]
        keep = [(vid, r) for vid, r in results.items() if r["status"] == "keep"]
        delete = [(vid, r) for vid, r in results.items() if r["status"] == "delete"]
        unclassified = [(vid, r) for vid, r in results.items() if r["status"] == "unclassified"]
        total_keep += len(keep)
        total_delete += len(delete)
        total_unclassified += len(unclassified)

        lines.append(f"## {pkg_name}")
        lines.append(
            f"{len(results)} versions — keep {len(keep)}, delete {len(delete)}, "
            f"unclassified {len(unclassified)}"
        )
        lines.append("")

        for label, group in (("### Delete candidates", delete),
                              ("### Kept", keep),
                              ("### Unclassified (kept by default)", unclassified)):
            lines.append(label)
            if not group:
                lines.append("- (none)")
            for vid, r in sorted(group, key=lambda kv: kv[1]["created_at"], reverse=True):
                tags = ", ".join(r["tags"]) if r["tags"] else "(untagged)"
                lines.append(f"- id={vid} tags=[{tags}]")
                for reason in r["reasons"]:
                    lines.append(f"    - {reason}")
            lines.append("")

    lines.append("## Totals")
    lines.append(f"keep={total_keep} delete={total_delete} unclassified={total_unclassified}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GitHub API I/O
# ---------------------------------------------------------------------------


def _api_request(url: str, token: str | None, method: str = "GET") -> urllib.request.Request:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers, method=method)


def _parse_next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        section = [s.strip() for s in part.split(";")]
        if len(section) < 2:
            continue
        url_part = section[0]
        if url_part.startswith("<") and url_part.endswith(">"):
            url_part = url_part[1:-1]
        if section[1] == 'rel="next"':
            return url_part
    return None


def paginated_get(url: str, token: str | None) -> list:
    """GET a paginated GitHub API list endpoint, following Link: rel=next."""
    results: list = []
    while url:
        req = _api_request(url, token)
        try:
            with urllib.request.urlopen(req) as resp:
                page = json.loads(resp.read().decode("utf-8"))
                results.extend(page)
                url = _parse_next_link(resp.headers.get("Link"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise SystemExit(f"GET {url} failed: {e.code} {e.reason}\n{body}") from e
    return results


def list_org_container_packages(org: str, token: str | None) -> list:
    """Names starting 'hal0-' only — never touch non-runner packages."""
    url = f"{API_ROOT}/orgs/{org}/packages?package_type=container&per_page=100"
    packages = paginated_get(url, token)
    return [p for p in packages if p.get("name", "").startswith("hal0-")]


def list_package_versions(org: str, package_name: str, token: str | None) -> list:
    url = f"{API_ROOT}/orgs/{org}/packages/container/{package_name}/versions?per_page=100"
    raw = paginated_get(url, token)
    versions = []
    for v in raw:
        tags = v.get("metadata", {}).get("container", {}).get("tags", []) or []
        versions.append(
            {
                "id": v["id"],
                "digest": v.get("name"),
                "created_at": parse_github_datetime(v["created_at"]),
                "tags": tags,
            }
        )
    return versions


def delete_package_version(org: str, package_name: str, version_id: int, token: str | None) -> tuple[bool, str]:
    url = f"{API_ROOT}/orgs/{org}/packages/container/{package_name}/versions/{version_id}"
    req = _api_request(url, token, method="DELETE")
    try:
        with urllib.request.urlopen(req):
            return True, "deleted"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return False, f"{e.code} {e.reason}: {body}"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def load_json_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--org", default=DEFAULT_ORG, help=f"GitHub org (default: {DEFAULT_ORG})")
    parser.add_argument(
        "--images-json",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "images.json"),
        help="Path to images.json (default: repo root images.json)",
    )
    parser.add_argument(
        "--allowlist",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "retention-allowlist.json"
        ),
        help="Path to retention-allowlist.json (default: repo root retention-allowlist.json)",
    )
    parser.add_argument(
        "--keep-releases", type=int, default=DEFAULT_KEEP_RELEASES,
        help=f"Newest N release-shaped tags to keep per package (default: {DEFAULT_KEEP_RELEASES})",
    )
    parser.add_argument(
        "--grace-days", type=int, default=DEFAULT_GRACE_DAYS,
        help=f"Never delete anything younger than this many days (default: {DEFAULT_GRACE_DAYS})",
    )
    parser.add_argument(
        "--delete", action="store_true",
        help="Actually perform deletions. Default is dry-run (report only).",
    )
    parser.add_argument(
        "--report-file", default="retention-report.md",
        help="Where to write the markdown report (default: retention-report.md)",
    )
    return parser


def main(argv: list | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    images_doc = load_json_file(args.images_json)
    allowlist_doc = load_json_file(args.allowlist)
    images_index = build_images_index(images_doc)
    allowlist_index = build_allowlist_index(allowlist_doc)

    print(f"Listing container packages for org '{args.org}'...", file=sys.stderr)
    packages_meta = list_org_container_packages(args.org, token)
    print(f"Found {len(packages_meta)} hal0-* packages.", file=sys.stderr)

    packages: dict = {}
    for pkg in packages_meta:
        name = pkg["name"]
        print(f"  fetching versions for {name}...", file=sys.stderr)
        packages[name] = list_package_versions(args.org, name, token)

    all_results = classify_org(
        packages,
        images_index,
        allowlist_index,
        keep_releases=args.keep_releases,
        grace_days=args.grace_days,
    )

    report = render_report(
        args.org, all_results, dry_run=not args.delete,
        keep_releases=args.keep_releases, grace_days=args.grace_days,
    )
    print(report)
    with open(args.report_file, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"Report written to {args.report_file}", file=sys.stderr)

    if not args.delete:
        print("Dry-run complete. Pass --delete to actually remove delete-candidate versions.", file=sys.stderr)
        return 0

    had_failure = False
    for pkg_name, results in all_results.items():
        for vid, r in results.items():
            if r["status"] != "delete":
                continue
            tags = ", ".join(r["tags"]) if r["tags"] else "(untagged)"
            print(f"DELETE {pkg_name} id={vid} tags=[{tags}] ...", file=sys.stderr)
            ok, detail = delete_package_version(args.org, pkg_name, vid, token)
            if ok:
                print(f"  ok: {detail}", file=sys.stderr)
            else:
                had_failure = True
                print(f"  FAILED: {detail}", file=sys.stderr)

    return 1 if had_failure else 0


if __name__ == "__main__":
    sys.exit(main())
