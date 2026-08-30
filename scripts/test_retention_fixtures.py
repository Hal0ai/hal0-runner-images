#!/usr/bin/env python3
"""Fixture tests for scripts/retention.py's classification core.

Deliberately NOT pytest — run directly with `python3` so it needs nothing
beyond the stdlib, matching retention.py itself:

    python3 scripts/test_retention_fixtures.py

Exercises classify_package() against small synthetic version sets. Each
test asserts a single version's resolved status (and, where it matters,
that a specific reason string shows up) so a failure points straight at
which rule broke.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from retention import (  # noqa: E402
    build_allowlist_index,
    build_images_index,
    classify_package,
    parse_allowlist_ref,
    render_outcomes_section,
)

NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


def days_ago(n: int) -> datetime:
    return NOW - timedelta(days=n)


def v(id_, digest, tags, age_days):
    return {"id": id_, "digest": digest, "tags": tags, "created_at": days_ago(age_days)}


FAILURES = []


def check(name, results, version_id, expected_status, reason_substring=None):
    r = results.get(version_id)
    if r is None:
        FAILURES.append(f"{name}: version id={version_id} missing from results")
        return
    if r["status"] != expected_status:
        FAILURES.append(
            f"{name}: expected status={expected_status!r} for id={version_id}, got {r['status']!r} "
            f"(reasons={r['reasons']})"
        )
        return
    if reason_substring is not None:
        if not any(reason_substring in reason for reason in r["reasons"]):
            FAILURES.append(
                f"{name}: expected a reason containing {reason_substring!r} for id={version_id}, "
                f"got {r['reasons']}"
            )
    print(f"ok: {name}")


def check_text(name, text, must_contain=(), must_not_contain=()):
    problems = [f"missing {s!r}" for s in must_contain if s not in text]
    problems += [f"unexpectedly contains {s!r}" for s in must_not_contain if s in text]
    if problems:
        FAILURES.append(f"{name}: {'; '.join(problems)}\n--- text was ---\n{text}")
        return
    print(f"ok: {name}")


def check_raises(name, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except SystemExit as e:
        print(f"ok: {name} (raised SystemExit: {e})")
        return
    FAILURES.append(f"{name}: expected SystemExit, but no exception was raised")


# ---------------------------------------------------------------------------
# Test 1: images.json-named tag is kept.
# ---------------------------------------------------------------------------

images_doc = {
    "images": [
        {"image": "ghcr.io/hal0ai/hal0-toolbox-cpu", "tag": "v1"},
    ]
}
images_index = build_images_index(images_doc)
allowlist_index = build_allowlist_index({"hal0_code_pins": [], "evidence": {"refs": []}})

versions = [
    v(1, "sha256:" + "a" * 64, ["v1"], age_days=400),  # named in images.json
]
results = classify_package("hal0-toolbox-cpu", versions, images_index, allowlist_index, now=NOW)
check("images.json-named tag kept", results, 1, "keep", "images.json")


# ---------------------------------------------------------------------------
# Test 2: allowlist-named tag is kept.
# ---------------------------------------------------------------------------

allow_doc = {
    "hal0_code_pins": ["ghcr.io/hal0ai/hal0-combined:0826"],
    "evidence": {"refs": ["ghcr.io/hal0ai/hal0-rocmfpx:c077206"]},
}
allowlist_index2 = build_allowlist_index(allow_doc)
empty_images_index = build_images_index({"images": []})

versions = [
    v(1, "sha256:" + "b" * 64, ["0826"], age_days=400),
]
results = classify_package("hal0-combined", versions, empty_images_index, allowlist_index2, now=NOW)
check("allowlist-named tag kept (hal0_code_pins)", results, 1, "keep", "allowlist")

versions = [
    v(1, "sha256:" + "c" * 64, ["c077206"], age_days=400),
]
results = classify_package("hal0-rocmfpx", versions, empty_images_index, allowlist_index2, now=NOW)
check("allowlist-named tag kept (evidence.refs)", results, 1, "keep", "allowlist")


# ---------------------------------------------------------------------------
# Test 3: newest-4 numeric release tags kept; 5th oldest numeric tag falls to
# unclassified (NOT deleted — releases beyond newest-N are kept by default
# in v1, only CI/cosign debris is auto-deleted).
# ---------------------------------------------------------------------------

versions = [
    v(1, "sha256:" + "1" * 64, ["100"], age_days=10),
    v(2, "sha256:" + "2" * 64, ["101"], age_days=20),
    v(3, "sha256:" + "3" * 64, ["102"], age_days=30),
    v(4, "sha256:" + "4" * 64, ["103"], age_days=40),
    v(5, "sha256:" + "5" * 64, ["99"], age_days=400),  # 5th newest by created_at -> oldest here
]
results = classify_package(
    "hal0-releasey", versions, empty_images_index, allowlist_index2, keep_releases=4, now=NOW
)
check("newest-4 numeric tag #1 kept", results, 1, "keep", "newest 4")
check("newest-4 numeric tag #2 kept", results, 2, "keep", "newest 4")
check("newest-4 numeric tag #3 kept", results, 3, "keep", "newest 4")
check("newest-4 numeric tag #4 kept", results, 4, "keep", "newest 4")
check("5th-oldest numeric tag -> unclassified, kept by default", results, 5, "unclassified", "kept by default")


# ---------------------------------------------------------------------------
# Test 4: plain CI sha tag, old -> delete.
# ---------------------------------------------------------------------------

versions = [
    v(1, "sha256:" + "d" * 64, ["sha-a1b2c3d"], age_days=400),
]
results = classify_package("hal0-toolbox-cpu-ci", versions, empty_images_index, allowlist_index2, now=NOW)
check("old ci sha tag deleted", results, 1, "delete", "CI sha")


# ---------------------------------------------------------------------------
# Test 5: young CI sha tag (<14d) -> kept (grace window).
# ---------------------------------------------------------------------------

versions = [
    v(1, "sha256:" + "e" * 64, ["sha-a1b2c3d"], age_days=3),
]
results = classify_package("hal0-toolbox-cpu-ci2", versions, empty_images_index, allowlist_index2, now=NOW)
check("young ci sha tag kept (grace window)", results, 1, "keep", "grace window")


# ---------------------------------------------------------------------------
# Test 6: cosign sig whose subject is KEPT -> cosign kept too.
# ---------------------------------------------------------------------------

subject_digest_hex = "f" * 64
subject_digest = f"sha256:{subject_digest_hex}"
versions = [
    v(1, subject_digest, ["v1"], age_days=400),  # kept via images.json
    v(2, "sha256:" + "0" * 64, [f"sha256-{subject_digest_hex}.sig"], age_days=400),
]
images_index3 = build_images_index({"images": [{"image": "ghcr.io/hal0ai/hal0-signed", "tag": "v1"}]})
results = classify_package("hal0-signed", versions, images_index3, allowlist_index2, now=NOW)
check("cosign sig with kept subject -> subject kept", results, 1, "keep", "images.json")
check("cosign sig with kept subject -> cosign kept too", results, 2, "keep", "subject")


# ---------------------------------------------------------------------------
# Test 7: cosign sig whose subject is DELETED (old ci sha, no keep rule) ->
# cosign deleted too.
# ---------------------------------------------------------------------------

subject_digest_hex2 = "9" * 64
subject_digest2 = f"sha256:{subject_digest_hex2}"
versions = [
    v(1, subject_digest2, ["sha-deadbee"], age_days=400),  # no keep rule -> delete
    v(2, "sha256:" + "8" * 64, [f"sha256-{subject_digest_hex2}.sig"], age_days=400),
]
results = classify_package("hal0-signed2", versions, empty_images_index, allowlist_index2, now=NOW)
check("subject with no keep rule -> deleted", results, 1, "delete", "CI sha")
check("cosign sig with deleted subject -> deleted too", results, 2, "delete", "deleted or absent")


# ---------------------------------------------------------------------------
# Test 7b: cosign sig whose subject is entirely ABSENT from the package ->
# cosign deleted too (orphaned signature).
# ---------------------------------------------------------------------------

versions = [
    v(1, "sha256:" + "7" * 64, ["sha256-" + ("6" * 64) + ".att"], age_days=400),
]
results = classify_package("hal0-signed3", versions, empty_images_index, allowlist_index2, now=NOW)
check("cosign att with absent subject -> deleted", results, 1, "delete", "absent")


# ---------------------------------------------------------------------------
# Test 8: untagged version -> always kept, regardless of age.
# ---------------------------------------------------------------------------

versions = [
    v(1, "sha256:" + "5" * 63 + "a", [], age_days=1000),
]
results = classify_package("hal0-multiarch", versions, empty_images_index, allowlist_index2, now=NOW)
check("untagged version kept regardless of age", results, 1, "keep", "untagged")


# ---------------------------------------------------------------------------
# Test 9: mutable pointer tags (main/edge/etc.) always kept.
# ---------------------------------------------------------------------------

versions = [
    v(1, "sha256:" + "3" * 63 + "a", ["main"], age_days=1000),
    v(2, "sha256:" + "4" * 63 + "a", ["edge"], age_days=1000),
]
results = classify_package("hal0-pointers", versions, empty_images_index, allowlist_index2, now=NOW)
check("main tag kept", results, 1, "keep", "mutable pointer")
check("edge tag kept", results, 2, "keep", "mutable pointer")


# ---------------------------------------------------------------------------
# Test 10: digest-form allowlist ref ("repo@sha256:<hex>") protects the
# version by digest, even though its only tag is CI-shaped and would
# otherwise be deleted.
# ---------------------------------------------------------------------------

digest_hex = "5" * 64
allow_digest_doc = {
    "hal0_code_pins": [f"ghcr.io/hal0ai/hal0-digestpin@sha256:{digest_hex}"],
    "evidence": {"refs": []},
}
allowlist_index_digest = build_allowlist_index(allow_digest_doc)

versions = [
    v(1, f"sha256:{digest_hex}", ["sha-a1b2c3d"], age_days=400),
]
results = classify_package("hal0-digestpin", versions, empty_images_index, allowlist_index_digest, now=NOW)
check("digest-form allowlist ref protects the version", results, 1, "keep", "retention-allowlist.json: pinned digest")


# ---------------------------------------------------------------------------
# Test 11: malformed allowlist refs abort loudly (SystemExit), never
# silently mis-key into a useless allowlist entry.
# ---------------------------------------------------------------------------

check_raises(
    "ref with no colon/@ aborts",
    parse_allowlist_ref,
    "ghcr.io/hal0ai/hal0-nocolon",
)
check_raises(
    "digest-form ref with malformed digest aborts",
    parse_allowlist_ref,
    "ghcr.io/hal0ai/hal0-baddigest@sha256:not-hex",
)
check_raises(
    "digest-form ref with empty digest aborts",
    parse_allowlist_ref,
    "ghcr.io/hal0ai/hal0-emptydigest@",
)
check_raises(
    "tag-form ref with empty tag aborts",
    parse_allowlist_ref,
    "ghcr.io/hal0ai/hal0-emptytag:",
)
check_raises(
    "build_allowlist_index propagates the SystemExit from a bad ref",
    build_allowlist_index,
    {"hal0_code_pins": ["not-a-valid-ref"], "evidence": {"refs": []}},
)


# ---------------------------------------------------------------------------
# Test 12 (safety-critical): a version carrying BOTH a sha-<hex> CI tag AND
# an unrecognized tag must NOT be deleted — the "every tag must match a
# debris shape" rule is per-VERSION, not per-tag. One unrecognized tag on
# an otherwise CI-shaped version means the whole version is unclassified.
# ---------------------------------------------------------------------------

versions = [
    v(1, "sha256:" + "6" * 64, ["sha-a1b2c3d", "banana"], age_days=400),
]
results = classify_package("hal0-mixedtags", versions, empty_images_index, allowlist_index2, now=NOW)
check(
    "mixed sha-ci + unrecognized tag -> unclassified, never deleted",
    results, 1, "unclassified", "kept by default",
)


# ---------------------------------------------------------------------------
# Test 13 (safety-critical): digest match keeps a version regardless of
# which of its tags images.json names. The version carries two tags,
# NEITHER of which equals the catalogued tag ("v9") — only the digest
# matches — and it must still be kept.
# ---------------------------------------------------------------------------

images_index_digest_only = build_images_index(
    {"images": [{"image": "ghcr.io/hal0ai/hal0-digestwins", "tag": "v9", "digest": "sha256:" + "7" * 64}]}
)
versions = [
    v(1, "sha256:" + "7" * 64, ["random-tag-a", "random-tag-b"], age_days=400),
]
results = classify_package("hal0-digestwins", versions, images_index_digest_only, allowlist_index2, now=NOW)
check(
    "digest match keeps version regardless of tag naming",
    results, 1, "keep", "images.json: pinned digest",
)


# ---------------------------------------------------------------------------
# Test 14: render_outcomes_section — the "Outcomes" block appended to the
# report after a --delete run. Mixed success/failure, untagged, and empty.
# ---------------------------------------------------------------------------

outcomes_text = render_outcomes_section(
    [
        {"package": "hal0-toolbox-cpu", "id": 11, "tags": ["sha-a1b2c3d"], "ok": True, "detail": "deleted"},
        {
            "package": "hal0-rocmfpx",
            "id": 22,
            "tags": [],
            "ok": False,
            "detail": "403 Forbidden: insufficient scope",
        },
    ]
)
check_text(
    "outcomes section: mixed success/failure",
    outcomes_text,
    must_contain=[
        "## Outcomes",
        "Executed 2 deletion(s): 1 deleted OK, 1 failed.",
        "- OK: hal0-toolbox-cpu id=11 tags=[sha-a1b2c3d] — deleted",
        "- FAILED: hal0-rocmfpx id=22 tags=[(untagged)] — 403 Forbidden: insufficient scope",
    ],
)

check_text(
    "outcomes section: empty run",
    render_outcomes_section([]),
    must_contain=[
        "Executed 0 deletion(s): 0 deleted OK, 0 failed.",
        "- (no delete candidates — nothing to execute)",
    ],
    must_not_contain=["- OK:", "- FAILED:"],
)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

if FAILURES:
    print(f"\n{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print(f" - {f}")
    sys.exit(1)

print(f"\nAll fixture checks passed.")
sys.exit(0)
