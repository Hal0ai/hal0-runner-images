#!/usr/bin/env python3
"""Offline tests for the CI scripts. stdlib only:

    python3 ci/test_ci.py

No network, no container runtime, no git remote: every GitHub call goes through
a fake `api` function, every file edit runs against a temp tree.
"""

from __future__ import annotations

import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402
import plan  # noqa: E402
import hal0_pins  # noqa: E402
import promote  # noqa: E402
import release_watch as rw  # noqa: E402

OLD_SHA = "c35b66744f13cb0dcc476af063e112122eee9355"
NEW_SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64

PIN = '''# The one llama.cpp release
[llama_cpp]
repo = "https://github.com/ggml-org/llama.cpp.git"
#: Release {version}: notes that describe the old release stay as written.
version = "{version}"   # release tag
ref = "{ref}"
ref_description = "release {version}: old subject"

[other]
version = "must-not-change"
'''
MANIFEST = '''# runners/{key}
[runner]
key = "{key}"

[source]
pin = "llama.cpp.toml"
'''


def make_tree(root: Path, runners: dict[str, dict], pin: dict | None = None) -> None:
    (root / "runners").mkdir(parents=True, exist_ok=True)
    if pin is not None:
        (root / "runners" / "llama.cpp.toml").write_text(PIN.format(**pin))
    for key, spec in runners.items():
        d = root / "runners" / key
        d.mkdir(parents=True)
        (d / "manifest.toml").write_text(spec.get("manifest") or MANIFEST.format(key=key))


class LibTests(unittest.TestCase):
    def test_tag_shape(self):
        self.assertTrue(lib.TAG_RE.match("b11552-r1a2b3c4"))
        self.assertTrue(lib.TAG_RE.match("0.9.44-r1a2b3c4"))
        for bad in ("latest", "b11552", "b11552-rXYZ1234", "-r1a2b3c4", "main"):
            self.assertFalse(lib.TAG_RE.match(bad), bad)

    def test_runner_keys_skip_underscore_and_manifestless(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_tree(root, {"standard": {}, "cpu": {}})
            (root / "runners" / "_x").mkdir()
            (root / "runners" / "empty").mkdir()
            self.assertEqual(lib.runner_keys(root), ["cpu", "standard"])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_tree(self.root, {"standard": {}, "cpu": {}, "kokoro": {"manifest": "[runner]\nkey = 'kokoro'\n"}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_shared_pin_file_selects_the_runners_that_name_it(self):
        keys = lib.runner_keys(self.root)
        self.assertEqual(plan.select_changed(keys, ["runners/llama.cpp.toml"], self.root), ["cpu", "standard"])
        self.assertEqual(plan.select_changed(keys, ["runners/other.toml"], self.root), [])

    def test_path_filter(self):
        keys = lib.runner_keys(self.root)
        self.assertEqual(plan.select_changed(keys, ["runners/cpu/manifest.toml", "README.md"]), ["cpu"])
        self.assertEqual(plan.select_changed(keys, ["docs/UPDATING.md", "runners/README.md"]), [])
        self.assertEqual(plan.select_changed(keys, ["runners/gone/Containerfile"]), [])
        self.assertEqual(plan.select_changed(keys, ["runners/kokoro/x.py", "runners/cpu/a", "runners/cpu/b"]), ["cpu", "kokoro"])

    def test_unknown_diff_builds_everything(self):
        keys = lib.runner_keys(self.root)
        self.assertEqual(plan.select_changed(keys, None), keys)
        self.assertIsNone(plan.changed_files("0" * 40, "HEAD", self.root))

    def test_explicit_select(self):
        keys = lib.runner_keys(self.root)
        self.assertEqual(plan.select_explicit(keys, "cpu, kokoro"), ["cpu", "kokoro"])
        self.assertEqual(plan.select_explicit(keys, "all"), keys)
        with self.assertRaises(SystemExit):
            plan.select_explicit(keys, "strix")

    def test_matrix_validation(self):
        good = lambda sel: {"include": [{"key": k, "image": f"ghcr.io/hal0ai/hal0-runner-{k}", "tag": "b1-r1234567"} for k in sel]}  # noqa: E731
        m = plan.build_matrix(["cpu"], good)
        self.assertEqual(m["include"][0]["timeout_minutes"], 360)
        self.assertFalse(m["include"][0]["heavy"])
        self.assertEqual(plan.build_matrix([], good), {"include": []})
        bad_image = lambda sel: {"include": [{"key": "cpu", "image": "ghcr.io/hal0ai/hal0-combined", "tag": "b1-r1234567"}]}  # noqa: E731
        bad_tag = lambda sel: {"include": [{"key": "cpu", "image": "ghcr.io/hal0ai/hal0-runner-cpu", "tag": "latest"}]}  # noqa: E731
        wrong_set = lambda sel: good(["standard"])  # noqa: E731
        for lister in (bad_image, bad_tag, wrong_set):
            with self.assertRaises(SystemExit):
                plan.build_matrix(["cpu"], lister)


class ReleaseWatchTests(unittest.TestCase):
    def test_version_compare(self):
        self.assertTrue(rw.is_newer("b11552", "b11510"))
        self.assertFalse(rw.is_newer("b11510", "b11510"))
        self.assertFalse(rw.is_newer("b9999", "b11510"))
        self.assertFalse(rw.is_newer("nightly", "b1"))

    def _api(self, latest="b11552"):
        rels = [
            {"tag_name": "b11551", "html_url": "u51", "prerelease": True, "draft": False},
            {"tag_name": latest, "html_url": "https://x/rel", "prerelease": True, "draft": False},
            {"tag_name": "b99999", "html_url": "u", "prerelease": True, "draft": True},
            {"tag_name": "v0.6.0", "html_url": "u06", "prerelease": False, "draft": False},
        ]

        def api(path):
            if "releases?per_page" in path:
                return rels
            if "/commits/" in path:
                return {"sha": NEW_SHA, "commit": {"message": "server: fix a thing (#1)\n\nlong body"}}
            raise AssertionError(f"/releases/latest must not be used for llama.cpp: {path}")
        return api

    def _catalogue(self, root, tag=None, candidate=None):
        (root / "images.json").write_text(json.dumps({"schema": "hal0.runner-images.v2", "runners": [
            {"id": "standard", "tag": None, "digest": None},
            {"id": "cuda", "image": "ghcr.io/ggml-org/llama.cpp", "tag": tag, "digest": None, "candidate": candidate},
        ]}, indent=2) + "\n")

    def test_takes_highest_b_tag_including_prereleases_not_drafts(self):
        self.assertEqual(rw.latest_release(self._api())["tag_name"], "b11552")

    def test_rewrite_pin_touches_only_the_pin_table_and_keeps_comments(self):
        text = PIN.format(version="b11510", ref=OLD_SHA)
        new, n = rw.rewrite_pin(text, {"version": "b11552", "ref": NEW_SHA, "ref_description": 'release b11552: say "hi"'})
        self.assertEqual(n, 3)
        doc = tomllib.loads(new)
        self.assertEqual((doc["llama_cpp"]["version"], doc["llama_cpp"]["ref"]), ("b11552", NEW_SHA))
        self.assertEqual(doc["llama_cpp"]["ref_description"], 'release b11552: say "hi"')
        self.assertEqual(doc["other"]["version"], "must-not-change")
        self.assertIn("#: Release b11510: notes", new)
        self.assertIn("# release tag", new)

    def test_bump_rewrites_only_the_pin_file(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_tree(root, {"standard": {}, "cpu": {}}, pin={"version": "b11510", "ref": OLD_SHA})
            before = (root / "runners/standard/manifest.toml").read_text()
            bump = rw.run(root, self._api(), apply=True)

            self.assertEqual((bump.previous, bump.tag, bump.commit), ("b11510", "b11552", NEW_SHA))
            self.assertEqual(bump.files, ["runners/llama.cpp.toml"])
            self.assertEqual((bump.branch, bump.title), ("bot/bump-llama-cpp-b11552", "chore(runners): bump llama.cpp to b11552"))
            pin = tomllib.loads((root / "runners/llama.cpp.toml").read_text())["llama_cpp"]
            self.assertEqual((pin["version"], pin["ref"]), ("b11552", NEW_SHA))
            self.assertEqual(pin["ref_description"], "release b11552: server: fix a thing (#1)")
            self.assertEqual((root / "runners/standard/manifest.toml").read_text(), before)

    def test_current_pin_means_no_bump_and_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_tree(root, {}, pin={"version": "b11552", "ref": NEW_SHA})
            self.assertIsNone(rw.run(root, self._api(), apply=True))
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_tree(root, {}, pin={"version": "b11510", "ref": OLD_SHA})
            before = (root / "runners/llama.cpp.toml").read_text()
            self.assertEqual(rw.run(root, self._api(), apply=False).tag, "b11552")
            self.assertEqual((root / "runners/llama.cpp.toml").read_text(), before)

    def test_missing_pin_file_fails_loudly(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(SystemExit):
                rw.run(Path(t), self._api(), apply=True)

    def test_newest_cuda_tag_not_ahead_of_the_release(self):
        tags = ["server-cuda-b11541", "server-cuda-b11515", "server-cuda-b11553", "server-cuda12-b11552",
                "server-b11552", "full-cuda-b11552", "server-cuda-b9"]
        self.assertEqual(rw.newest_cuda_tag(tags, "b11552"), "server-cuda-b11541")
        self.assertEqual(rw.newest_cuda_tag(tags, "b11553"), "server-cuda-b11553")
        self.assertEqual(rw.newest_cuda_tag(tags, "b11500"), "server-cuda-b9")
        self.assertIsNone(rw.newest_cuda_tag(["server-b1"], "b11552"))

    def test_bump_sets_the_cuda_candidate_to_the_newest_tag_that_exists(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_tree(root, {"standard": {}}, pin={"version": "b11510", "ref": OLD_SHA})
            self._catalogue(root, tag="server-cuda-b11515")
            tags = ["server-cuda-b11515", "server-cuda-b11541", "server-cuda-b11553"]  # b11553 is ahead of b11552
            bump = rw.run(root, self._api(), apply=True, cuda_tags=lambda: tags, cuda_digest=lambda tag: "sha256:" + "d" * 64)
            self.assertEqual((bump.tag, bump.cuda_tag), ("b11552", "server-cuda-b11541"))
            self.assertEqual(bump.title, "chore(runners): bump llama.cpp to b11552 and cuda to server-cuda-b11541")
            self.assertEqual(bump.branch, "bot/bump-llama-cpp-b11552-cuda-b11541")
            self.assertEqual(sorted(bump.files), ["images.json", "runners/llama.cpp.toml"])
            row = json.loads((root / "images.json").read_text())["runners"][1]
            self.assertEqual(row["candidate"], {"tag": "server-cuda-b11541", "digest": "sha256:" + "d" * 64})
            self.assertEqual(row["tag"], "server-cuda-b11515")  # the pin moves only through promote
            self.assertIn("server-cuda-b11541", rw.pr_body(bump))

    def test_cuda_is_only_offered_when_newer_than_the_pin_and_the_candidate(self):
        digest = lambda tag: "sha256:" + "d" * 64  # noqa: E731
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_tree(root, {"standard": {}}, pin={"version": "b11552", "ref": NEW_SHA})
            # already pinned at the newest tag: nothing to do
            self._catalogue(root, tag="server-cuda-b11541")
            self.assertIsNone(rw.run(root, self._api(), apply=True, cuda_tags=lambda: ["server-cuda-b11541"], cuda_digest=digest))
            # pinned older, candidate already the newest: nothing to do either
            self._catalogue(root, tag="server-cuda-b11515", candidate={"tag": "server-cuda-b11541", "digest": DIGEST})
            self.assertIsNone(rw.run(root, self._api(), apply=True, cuda_tags=lambda: ["server-cuda-b11541"], cuda_digest=digest))
            # pinned older, no candidate: a cuda-only PR that leaves the pin file alone
            self._catalogue(root, tag="server-cuda-b11515")
            before = (root / "runners/llama.cpp.toml").read_text()
            bump = rw.run(root, self._api(), apply=True, cuda_tags=lambda: ["server-cuda-b11541"], cuda_digest=digest)
            self.assertIsNone(bump.tag)
            self.assertEqual((bump.branch, bump.files), ("bot/bump-llama-cpp-cuda-b11541", ["images.json"]))
            self.assertEqual((root / "runners/llama.cpp.toml").read_text(), before)


class PromoteTests(unittest.TestCase):
    RESULT = {"runner": "standard", "image": "ghcr.io/hal0ai/hal0-runner-standard",
              "tag": "b11552-r1a2b3c4", "digest": DIGEST,
              "gate_report": "https://example.test/report.json",
              "lanes_gated": ["rocm", "vulkan"]}

    def test_edit_catalogue_moves_candidate_to_pin_and_touches_nothing_else(self):
        raw = json.dumps({"schema": "hal0.runner-images.v2", "runners": [
            {"key": "standard", "image": self.RESULT["image"], "tag": "old", "digest": "sha256:" + "0" * 64,
             "candidate": {"tag": self.RESULT["tag"], "digest": DIGEST}, "gate_report": None,
             "lanes_gated": [], "summary": "é"},
            {"key": "cpu", "image": "ghcr.io/hal0ai/hal0-runner-cpu", "tag": "t", "candidate": None},
        ]}, indent=2, ensure_ascii=False) + "\n"
        out = json.loads(promote.edit_catalogue(raw, [self.RESULT]))
        std, cpu = out["runners"]
        self.assertEqual((std["tag"], std["digest"]), (self.RESULT["tag"], DIGEST))
        self.assertIsNone(std["candidate"])
        self.assertEqual(std["gate_report"], "https://example.test/report.json")
        self.assertEqual(std["lanes_gated"], ["rocm", "vulkan"])
        self.assertEqual(std["summary"], "é")
        self.assertEqual(cpu, {"key": "cpu", "image": "ghcr.io/hal0ai/hal0-runner-cpu", "tag": "t", "candidate": None})
        self.assertTrue(promote.edit_catalogue(raw, [self.RESULT]).endswith("}\n"))

    def test_gate_lanes_must_be_backends_of_the_runner(self):
        raw = json.dumps({"runners": [{"id": "standard", "image": self.RESULT["image"], "backends": ["vulkan"],
                                       "tag": None, "digest": None, "lanes_gated": []}]})
        with self.assertRaises(SystemExit):  # the gate report passed rocm, which standard (here) does not list
            promote.edit_catalogue(raw, [self.RESULT])

    def test_catalogue_without_optional_fields_gets_only_tag_and_digest(self):
        raw = json.dumps({"schema": "hal0.runner-images.v2", "runners": [
            {"id": "standard", "image": self.RESULT["image"], "tag": None, "digest": None, "title": "Standard"}]}, indent=2) + "\n"
        row = json.loads(promote.edit_catalogue(raw, [self.RESULT]))["runners"][0]
        self.assertEqual(row, {"id": "standard", "image": self.RESULT["image"], "tag": self.RESULT["tag"],
                               "digest": DIGEST, "title": "Standard"})

    def test_edit_catalogue_rejects_unknown_or_mismatched_runner(self):
        raw = json.dumps({"runners": [{"key": "cpu", "image": "ghcr.io/hal0ai/hal0-runner-cpu"}]})
        with self.assertRaises(SystemExit):
            promote.edit_catalogue(raw, [self.RESULT])
        raw = json.dumps({"runners": [{"key": "standard", "image": "ghcr.io/hal0ai/hal0-combined"}]})
        with self.assertRaises(SystemExit):
            promote.edit_catalogue(raw, [self.RESULT])

    def test_dump_like_keeps_the_file_style(self):
        for kwargs in ({"indent": 2, "ensure_ascii": True}, {"indent": 4, "ensure_ascii": False}):
            raw = json.dumps({"a": "é—", "b": [1]}, **kwargs) + "\n"
            self.assertEqual(lib.dump_like(json.loads(raw), raw), raw)

    def test_hal0_pin_edit_updates_one_row_and_keeps_the_rest(self):
        raw = json.dumps({"version": 1, hal0_pins.HAL0_PIN_TABLE: {
            "standard": {"image": "old", "tag": "old", "digest": "old", "lanes_gated": [], "_notes": "keep me"},
            "cpu": {"image": "i", "tag": "t", "digest": "d", "lanes_gated": ["cpu"]},
        }}, indent=2) + "\n"
        out = json.loads(promote.edit_hal0_pins(raw, [self.RESULT]))
        row = out[hal0_pins.HAL0_PIN_TABLE]["standard"]
        self.assertEqual(row[hal0_pins.HAL0_FIELDS["digest"]], DIGEST)
        self.assertEqual(row[hal0_pins.HAL0_FIELDS["image"]], self.RESULT["image"])
        self.assertEqual(row[hal0_pins.HAL0_FIELDS["tag"]], self.RESULT["tag"])
        self.assertEqual(row[hal0_pins.HAL0_FIELDS["lanes_gated"]], ["rocm", "vulkan"])
        self.assertEqual(row["_notes"], "keep me")
        self.assertEqual(out[hal0_pins.HAL0_PIN_TABLE]["cpu"]["tag"], "t")

    def test_hal0_pin_edit_fails_closed(self):
        raw = json.dumps({hal0_pins.HAL0_PIN_TABLE: {}})
        with self.assertRaises(SystemExit):
            promote.edit_hal0_pins(raw, [self.RESULT])
        created = json.loads(promote.edit_hal0_pins(raw, [self.RESULT], create_missing=True))
        self.assertIn("standard", created[hal0_pins.HAL0_PIN_TABLE])
        with self.assertRaises(SystemExit):
            promote.edit_hal0_pins(json.dumps({"other": {}}), [self.RESULT])

    def test_pin_refs_and_parse_pin_table(self):
        raw = json.dumps({hal0_pins.HAL0_PIN_TABLE: {
            "standard": {"image": "ghcr.io/hal0ai/hal0-runner-standard", "tag": "b1-r1234567", "digest": DIGEST},
            "cpu": {"image": "ghcr.io/hal0ai/hal0-runner-cpu", "tag": None, "digest": None},
        }})
        self.assertEqual(hal0_pins.parse_pin_table(raw, "u"), [
            "ghcr.io/hal0ai/hal0-runner-standard:b1-r1234567", f"ghcr.io/hal0ai/hal0-runner-standard@{DIGEST}"])
        for bad in ("not json", "[]", "{}", json.dumps({hal0_pins.HAL0_PIN_TABLE: {}}),
                    json.dumps({hal0_pins.HAL0_PIN_TABLE: {"a": "x"}}),
                    json.dumps({hal0_pins.HAL0_PIN_TABLE: {"a": {"tag": "t"}}}),
                    json.dumps({hal0_pins.HAL0_PIN_TABLE: {"a": {"image": "ghcr.io/x/y:t", "tag": "t"}}})):
            with self.assertRaises(SystemExit):
                hal0_pins.parse_pin_table(bad, "u")

    def test_parse_promotions(self):
        d2 = "sha256:" + "c" * 64
        items = promote.parse_promotions(f"standard={DIGEST}, cpu={d2} cuda=server-cuda-b11552@{DIGEST}")
        self.assertEqual([i["runner"] for i in items], ["standard", "cpu", "cuda"])
        self.assertEqual((items[2]["tag"], items[2]["digest"]), ("server-cuda-b11552", DIGEST))
        self.assertEqual(items[0]["tag"], "")
        for bad in ("", "standard", "standard=latest", f"standard={DIGEST},standard={DIGEST}", "Standard=" + DIGEST):
            with self.assertRaises(SystemExit):
                promote.parse_promotions(bad)

    def test_parse_gate_reports(self):
        keys = ["standard", "cpu"]
        one = promote.parse_gate_reports("https://x.test/g.json", keys)
        self.assertEqual(one, {"standard": "https://x.test/g.json", "cpu": "https://x.test/g.json"})
        mixed = promote.parse_gate_reports("https://x.test/all.json, cpu=https://x.test/cpu.json?a=b", keys)
        self.assertEqual(mixed["cpu"], "https://x.test/cpu.json?a=b")
        self.assertEqual(mixed["standard"], "https://x.test/all.json")
        for text in ("cpu=https://x.test/c.json",          # standard has no report
                     "https://a.test/1 https://b.test/2",  # two bare URLs
                     "http://x.test/g.json",
                     "cuda=https://x.test/g.json https://x.test/all.json"):  # cuda is not being promoted
            with self.assertRaises(SystemExit):
                promote.parse_gate_reports(text, keys)

    def test_external_runner_may_come_without_a_gate_report(self):
        keys = ["standard", "cuda"]
        urls = promote.parse_gate_reports("standard=https://x.test/s.json", keys, frozenset({"cuda"}))
        self.assertEqual(urls, {"standard": "https://x.test/s.json", "cuda": None})
        with self.assertRaises(SystemExit):  # a built runner always needs one
            promote.parse_gate_reports("cuda=https://x.test/c.json", keys, frozenset({"cuda"}))
        self.assertTrue(promote.has_entry({"runners": [{"runner": "cuda"}]}, "cuda"))
        self.assertFalse(promote.has_entry({"runners": [{"runner": "standard"}]}, "cuda"))
        # an ungated item still lands in images.json with a null report and no lanes
        raw = json.dumps({"runners": [{"id": "cuda", "image": "ghcr.io/ggml-org/llama.cpp", "backends": ["cuda"],
                                       "tag": None, "digest": None, "gate_report": None, "lanes_gated": []}]})
        item = {"runner": "cuda", "image": "ghcr.io/ggml-org/llama.cpp", "tag": "server-cuda-b11541", "digest": DIGEST,
                "external": True, "source_commit": "", "gate_report": None, "gate_summary": "no hardware gate", "lanes_gated": []}
        row = json.loads(promote.edit_catalogue(raw, [item]))["runners"][0]
        self.assertEqual((row["tag"], row["gate_report"], row["lanes_gated"]), ("server-cuda-b11541", None, []))
        body = promote.pr_body([item])
        self.assertNotIn("- gate report:", body)
        self.assertIn("UNGATED external image", body)

    def test_group_report_picks_each_runners_entry(self):
        def entry(k, d):
            return {"runner": k, "digest": d, "passed": True, "lanes": {"rocm": {"passed": True}}}
        d2 = "sha256:" + "c" * 64
        doc = {"schema": promote.GATE_SCHEMA, "runners": [entry("standard", DIGEST), entry("cpu", d2)]}
        self.assertEqual(promote.report_for(doc, "cpu")["digest"], d2)
        promote.validate_gate_report(promote.report_for(doc, "standard"), "standard", DIGEST)
        with self.assertRaises(SystemExit):
            promote.report_for(doc, "cuda")
        with self.assertRaises(SystemExit):
            promote.report_for({"runners": doc["runners"]}, "cpu")  # no schema

    def test_llama_group_standard_and_cpu_match_and_cuda_may_lag(self):
        def item(k, tag):
            return {"runner": k, "tag": tag, "digest": DIGEST}
        group = [item("standard", "b11552-r1a2b3c"), item("cpu", "b11552-r2b3c4d"), item("cuda", "server-cuda-b11541")]
        self.assertEqual(promote.check_group(group), 11552)
        branch, title = promote.describe(group)
        self.assertEqual(title, "chore(runners): pin standard, cpu, cuda to llama.cpp b11552 (cuda server-cuda-b11541)")
        self.assertTrue(branch.startswith("bot/promote-cpu-cuda-standard-"))
        self.assertEqual(promote.check_group([item("standard", "b11552-r1a2b3c"), item("cuda", "server-cuda-b11552")]), 11552)
        for bad in (
            [item("standard", "b11552-r1a2b3c"), item("cpu", "b11551-r2b3c4d")],        # standard/cpu differ
            [item("standard", "b11552-r1a2b3c"), item("cuda", "server-cuda-b11553")],   # cuda ahead of the group
            [item("cuda", "latest")],
            [item("standard", "latest")],
        ):
            with self.assertRaises(SystemExit):
                promote.check_group(bad)
        # cuda alone, or beside unrelated runners, has nothing to be ahead of
        self.assertIsNone(promote.check_group([item("cuda", "server-cuda-b11541")]))
        self.assertEqual(promote.describe([item("kokoro", "1.0-r1234567")]),
                         ("bot/promote-kokoro-1.0-r1234567", "chore(runners): pin kokoro to 1.0-r1234567"))
        self.assertIsNone(promote.check_group([item("kokoro", "1.0-r1234567"), item("flm", "0.9.44-r1234567")]))

    def test_group_edits_write_every_runner_into_one_file(self):
        d2 = "sha256:" + "c" * 64
        a = dict(self.RESULT)
        b = dict(self.RESULT, runner="cpu", image="ghcr.io/hal0ai/hal0-runner-cpu", digest=d2, lanes_gated=["cpu"])
        cat = json.dumps({"runners": [{"key": "standard", "image": a["image"]}, {"key": "cpu", "image": b["image"]}]})
        out = json.loads(promote.edit_catalogue(cat, [a, b]))["runners"]
        self.assertEqual([r["digest"] for r in out], [DIGEST, d2])
        pins = json.dumps({hal0_pins.HAL0_PIN_TABLE: {"standard": {}, "cpu": {}}})
        rows = json.loads(promote.edit_hal0_pins(pins, [a, b]))[hal0_pins.HAL0_PIN_TABLE]
        self.assertEqual(rows["cpu"]["lanes_gated"], ["cpu"])
        self.assertEqual(rows["standard"]["digest"], DIGEST)

    def test_retention_ignores_pins_outside_our_namespace(self):
        raw = json.dumps({hal0_pins.HAL0_PIN_TABLE: {
            "standard": {"image": "ghcr.io/hal0ai/hal0-runner-standard", "tag": "b1-r1234567", "digest": DIGEST},
            "cuda": {"image": "ghcr.io/ggml-org/llama.cpp", "tag": "server-cuda-b1", "digest": DIGEST},
        }})
        refs = hal0_pins.parse_pin_table(raw, "u")
        self.assertTrue(refs and all(r.startswith("ghcr.io/hal0ai/") for r in refs))
        only_external = json.dumps({hal0_pins.HAL0_PIN_TABLE: {"cuda": {"image": "ghcr.io/ggml-org/llama.cpp", "tag": "t"}}})
        with self.assertRaises(SystemExit):  # nothing of ours left: broken table, abort
            hal0_pins.parse_pin_table(only_external, "u")

    def test_gate_report_validation(self):
        good = {"schema": promote.GATE_SCHEMA, "runner": "standard", "digest": DIGEST, "passed": True,
                "lanes": {"rocm": {"passed": True}, "vulkan": {"passed": True}},
                "probes": [{"name": "health", "passed": True}]}
        summary, lanes = promote.validate_gate_report(good, "standard", DIGEST)
        self.assertIn("rocm, vulkan", summary)
        self.assertEqual(lanes, ["rocm", "vulkan"])
        for mutate in (
            lambda r: r.update(passed=False),
            lambda r: r.update(digest="sha256:" + "c" * 64),
            lambda r: r.update(runner="cpu"),
            lambda r: r.update(schema="other"),
            lambda r: r.update(probes=[{"name": "needle", "passed": False}]),
        ):
            bad = json.loads(json.dumps(good))
            mutate(bad)
            with self.assertRaises(SystemExit):
                promote.validate_gate_report(bad, "standard", DIGEST)


if __name__ == "__main__":
    unittest.main(verbosity=1)
