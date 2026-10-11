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

PIN = '''# shared llama.cpp pin
[llama_cpp]
repo = "https://github.com/ggml-org/llama.cpp"
version = "{version}"   # release tag
ref = "{ref}"
ref_description = "old words"

[other]
ref = "must-not-change"
'''
MANIFEST = '''# recipe; pin lives in runners/llama.cpp.toml
[source]
repo = "https://github.com/ggml-org/llama.cpp"
version = "{version}"
ref = "{ref}"
'''
CONTAINERFILE = '''FROM scratch
ARG LLAMA_VERSION={version}
ARG LLAMA_REF="{ref}"
ARG KEEP=other
RUN echo {ref}
'''


def make_tree(root: Path, runners: dict[str, dict], pin: dict | None = None) -> None:
    (root / "runners").mkdir(parents=True, exist_ok=True)
    if pin is not None:
        (root / "runners" / "llama.cpp.toml").write_text(PIN.format(**pin))
    for key, spec in runners.items():
        d = root / "runners" / key
        d.mkdir(parents=True)
        v, r = spec.get("version", "b11510"), spec.get("ref", OLD_SHA)
        (d / "manifest.toml").write_text(spec.get("manifest") or MANIFEST.format(version=v, ref=r))
        (d / "Containerfile").write_text(spec.get("containerfile") or CONTAINERFILE.format(version=v, ref=r))


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
        make_tree(self.root, {"standard": {}, "cpu": {}, "kokoro": {"manifest": "[source]\nversion='1'\n"}},
                  pin={"version": "b11510", "ref": OLD_SHA})
        # standard and cpu name the shared pin file; kokoro does not.
        for k in ("standard", "cpu"):
            (self.root / "runners" / k / "manifest.toml").write_text("# pin: llama.cpp.toml\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_path_filter(self):
        keys = lib.runner_keys(self.root)
        self.assertEqual(plan.select_changed(keys, ["runners/cpu/manifest.toml", "README.md"], self.root), ["cpu"])
        self.assertEqual(plan.select_changed(keys, ["docs/UPDATING.md", "runners/README.md"], self.root), [])
        self.assertEqual(plan.select_changed(keys, ["runners/gone/Containerfile"], self.root), [])

    def test_shared_pin_file_selects_the_runners_that_name_it(self):
        keys = lib.runner_keys(self.root)
        self.assertEqual(plan.select_changed(keys, ["runners/llama.cpp.toml"], self.root), ["cpu", "standard"])

    def test_unknown_diff_builds_everything(self):
        keys = lib.runner_keys(self.root)
        self.assertEqual(plan.select_changed(keys, None, self.root), keys)
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
                return {"sha": NEW_SHA}
            raise AssertionError(f"/releases/latest must not be used for llama.cpp: {path}")
        return api

    def test_takes_highest_b_tag_including_prereleases_not_drafts(self):
        self.assertEqual(rw.latest_release(self._api())["tag_name"], "b11552")

    def test_rewrite_table_touches_only_that_table(self):
        text = PIN.format(version="b11510", ref=OLD_SHA)
        new, n = rw.rewrite_table(text, "llama_cpp", {"version": "b11552", "ref": NEW_SHA, "ref_description": "d"})
        self.assertEqual(n, 3)
        doc = tomllib.loads(new)
        self.assertEqual((doc["llama_cpp"]["version"], doc["llama_cpp"]["ref"]), ("b11552", NEW_SHA))
        self.assertEqual(doc["other"]["ref"], "must-not-change")
        self.assertIn("# release tag", new)

    def test_rewrite_containerfile_args_only_matching_args(self):
        cf = CONTAINERFILE.format(version="b11510", ref=OLD_SHA)
        new, n = rw.rewrite_containerfile_args(cf, {OLD_SHA: NEW_SHA, "b11510": "b11552"})
        self.assertEqual(n, 2)
        self.assertIn("ARG LLAMA_VERSION=b11552", new)
        self.assertIn(f'ARG LLAMA_REF="{NEW_SHA}"', new)
        self.assertIn("ARG KEEP=other", new)
        self.assertIn(f"RUN echo {OLD_SHA}", new)  # not an ARG line: untouched

    def test_bump_moves_pin_and_every_mirror_together(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_tree(root, {
                "standard": {},
                "cpu": {"version": "b11510", "ref": "b11510"},  # tag-style mirror
                "kokoro": {"manifest": MANIFEST.replace("llama.cpp", "x").format(version="1.0", ref="v1.0"),
                           "containerfile": "FROM scratch\nARG V=1.0\n"},
            }, pin={"version": "b11510", "ref": OLD_SHA})
            bump = rw.run(root, self._api(), apply=True)

            self.assertEqual((bump.previous, bump.tag, bump.commit), ("b11510", "b11552", NEW_SHA))
            pin = tomllib.loads((root / "runners/llama.cpp.toml").read_text())["llama_cpp"]
            self.assertEqual((pin["version"], pin["ref"]), ("b11552", NEW_SHA))
            for key, ref in (("standard", NEW_SHA), ("cpu", NEW_SHA)):
                src = tomllib.loads((root / "runners" / key / "manifest.toml").read_text())["source"]
                self.assertEqual(src["version"], "b11552")
                self.assertIn(f'ARG LLAMA_VERSION=b11552', (root / "runners" / key / "Containerfile").read_text())
            # unrelated runner untouched
            self.assertIn('version = "1.0"', (root / "runners/kokoro/manifest.toml").read_text())
            self.assertNotIn("runners/kokoro/manifest.toml", bump.files)
            self.assertIn("runners/llama.cpp.toml", bump.files)

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
        out = json.loads(promote.edit_catalogue(raw, self.RESULT))
        std, cpu = out["runners"]
        self.assertEqual((std["tag"], std["digest"]), (self.RESULT["tag"], DIGEST))
        self.assertIsNone(std["candidate"])
        self.assertEqual(std["gate_report"], "https://example.test/report.json")
        self.assertEqual(std["lanes_gated"], ["rocm", "vulkan"])
        self.assertEqual(std["summary"], "é")
        self.assertEqual(cpu, {"key": "cpu", "image": "ghcr.io/hal0ai/hal0-runner-cpu", "tag": "t", "candidate": None})
        self.assertTrue(promote.edit_catalogue(raw, self.RESULT).endswith("}\n"))

    def test_edit_catalogue_rejects_unknown_or_mismatched_runner(self):
        raw = json.dumps({"runners": [{"key": "cpu", "image": "ghcr.io/hal0ai/hal0-runner-cpu"}]})
        with self.assertRaises(SystemExit):
            promote.edit_catalogue(raw, self.RESULT)
        raw = json.dumps({"runners": [{"key": "standard", "image": "ghcr.io/hal0ai/hal0-combined"}]})
        with self.assertRaises(SystemExit):
            promote.edit_catalogue(raw, self.RESULT)

    def test_dump_like_keeps_the_file_style(self):
        for kwargs in ({"indent": 2, "ensure_ascii": True}, {"indent": 4, "ensure_ascii": False}):
            raw = json.dumps({"a": "é—", "b": [1]}, **kwargs) + "\n"
            self.assertEqual(promote.dump_like(json.loads(raw), raw), raw)

    def test_hal0_pin_edit_updates_one_row_and_keeps_the_rest(self):
        raw = json.dumps({"version": 1, hal0_pins.HAL0_PIN_TABLE: {
            "standard": {"image": "old", "tag": "old", "digest": "old", "_notes": "keep me"},
            "cpu": {"image": "i", "tag": "t", "digest": "d"},
        }}, indent=2) + "\n"
        out = json.loads(promote.edit_hal0_pins(raw, self.RESULT))
        row = out[hal0_pins.HAL0_PIN_TABLE]["standard"]
        self.assertEqual(row[hal0_pins.HAL0_FIELDS["digest"]], DIGEST)
        self.assertEqual(row[hal0_pins.HAL0_FIELDS["image"]], self.RESULT["image"])
        self.assertEqual(row["_notes"], "keep me")
        self.assertEqual(out[hal0_pins.HAL0_PIN_TABLE]["cpu"]["tag"], "t")

    def test_hal0_pin_edit_fails_closed(self):
        raw = json.dumps({hal0_pins.HAL0_PIN_TABLE: {}})
        with self.assertRaises(SystemExit):
            promote.edit_hal0_pins(raw, self.RESULT)
        created = json.loads(promote.edit_hal0_pins(raw, self.RESULT, create_missing=True))
        self.assertIn("standard", created[hal0_pins.HAL0_PIN_TABLE])
        with self.assertRaises(SystemExit):
            promote.edit_hal0_pins(json.dumps({"other": {}}), self.RESULT)

    def test_pin_refs_and_parse_pin_table(self):
        raw = json.dumps({hal0_pins.HAL0_PIN_TABLE: {
            "standard": {"image": "ghcr.io/hal0ai/hal0-runner-standard", "tag": "b1-r1234567", "digest": DIGEST},
            "cpu": {"image": "ghcr.io/hal0ai/hal0-runner-cpu", "tag": None, "digest": None},
        }})
        self.assertEqual(hal0_pins.parse_pin_table(raw, "u"), [
            "ghcr.io/hal0ai/hal0-runner-standard:b1-r1234567", f"ghcr.io/hal0ai/hal0-runner-standard@{DIGEST}"])
        for bad in ("not json", "[]", "{}", json.dumps({hal0_pins.HAL0_PIN_TABLE: {}}),
                    json.dumps({hal0_pins.HAL0_PIN_TABLE: {"a": "x"}}),
                    json.dumps({hal0_pins.HAL0_PIN_TABLE: {"a": {"tag": "t"}}})):
            with self.assertRaises(SystemExit):
                hal0_pins.parse_pin_table(bad, "u")

    def test_gate_report_validation(self):
        good = {"schema": promote.GATE_SCHEMA, "runner": "standard", "digest": DIGEST, "passed": True,
                "lanes": {"rocm": {"passed": True}, "vulkan": {"passed": True}},
                "probes": [{"name": "health", "passed": True}]}
        self.assertIn("rocm, vulkan", promote.validate_gate_report(good, "standard", DIGEST))
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
