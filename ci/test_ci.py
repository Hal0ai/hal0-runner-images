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
import promote  # noqa: E402
import release_watch as rw  # noqa: E402

OLD_SHA = "c35b66744f13cb0dcc476af063e112122eee9355"
NEW_SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64

MANIFEST = '''# recipe
[source]
repo = "https://github.com/ggml-org/llama.cpp"
version = "{version}"   # tag prefix
ref = "{ref}"
ref_description = "old words"

[lifecycle]
ref = "must-not-change"
'''
CONTAINERFILE = '''FROM scratch
ARG LLAMA_VERSION={version}
ARG LLAMA_REF="{ref}"
ARG KEEP=other
RUN echo {ref}
'''


def make_tree(root: Path, runners: dict[str, dict]) -> None:
    for key, spec in runners.items():
        d = root / "runners" / key
        d.mkdir(parents=True)
        (d / "manifest.toml").write_text(spec.get("manifest") or MANIFEST.format(
            version=spec.get("version", "b11510"), ref=spec.get("ref", OLD_SHA)))
        (d / "Containerfile").write_text(spec.get("containerfile") or CONTAINERFILE.format(
            version=spec.get("version", "b11510"), ref=spec.get("ref", OLD_SHA)))


class LibTests(unittest.TestCase):
    def test_immutable_tag(self):
        self.assertEqual(lib.immutable_tag("b11552", "1a2b3c4d5e" * 4), "b11552-r1a2b3c4")
        self.assertTrue(lib.TAG_RE.match("0.9.44-r1a2b3c4"))
        for bad in ("latest", "b11552", "b11552-rXYZ1234", "-r1a2b3c4", "main"):
            self.assertFalse(lib.TAG_RE.match(bad), bad)

    def test_runner_keys_skip_shared_and_manifestless(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_tree(root, {"standard": {}, "cpu": {}})
            (root / "runners" / "_shared").mkdir()
            (root / "runners" / "empty").mkdir()
            self.assertEqual(lib.runner_keys(root), ["cpu", "standard"])

    def test_build_paths_reject_escape(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_tree(root, {"standard": {}})
            with self.assertRaises(SystemExit):
                lib.build_paths("standard", {"build": {"context": "../x"}}, root)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_tree(self.root, {"standard": {}, "cpu": {}, "kokoro": {}})
        # standard's Containerfile uses the shared entrypoint; kokoro does not.
        (self.root / "runners" / "standard" / "Containerfile").write_text("COPY runners/_shared/entrypoint.sh /\n")
        (self.root / "runners" / "_shared").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_path_filter(self):
        keys = lib.runner_keys(self.root)
        self.assertEqual(plan.select_changed(keys, ["runners/cpu/manifest.toml", "README.md"], self.root), ["cpu"])
        self.assertEqual(plan.select_changed(keys, ["docs/UPDATING.md", "runners/README.md"], self.root), [])
        self.assertEqual(plan.select_changed(keys, ["runners/gone/Containerfile"], self.root), [])

    def test_shared_selects_only_users(self):
        keys = lib.runner_keys(self.root)
        self.assertEqual(plan.select_changed(keys, ["runners/_shared/entrypoint.sh"], self.root), ["standard"])

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

    def test_matrix_fields(self):
        m = plan.build_matrix(["cpu"], "1234567" + "0" * 33, self.root)["include"][0]
        self.assertEqual(m["package"], "ghcr.io/hal0ai/hal0-runner-cpu")
        self.assertEqual(m["tag"], "b11510-r1234567")
        self.assertEqual(m["containerfile"], "runners/cpu/Containerfile")
        self.assertFalse(m["has_check"])


class ReleaseWatchTests(unittest.TestCase):
    def test_version_compare(self):
        self.assertTrue(rw.is_newer("b11552", "b11510"))
        self.assertFalse(rw.is_newer("b11510", "b11510"))
        self.assertFalse(rw.is_newer("b9999", "b11510"))
        self.assertTrue(rw.is_newer("v0.9.45", "0.9.44"))
        self.assertTrue(rw.same_version("v0.9.44", "0.9.44"))
        self.assertFalse(rw.is_newer("nightly", "b1"))

    def test_llama_cpp_uses_b_tags_including_prereleases(self):
        rels = [
            {"tag_name": "b11551", "html_url": "u51", "prerelease": True, "draft": False},
            {"tag_name": "b11552", "html_url": "u52", "prerelease": True, "draft": False},
            {"tag_name": "b11553", "html_url": "u53", "prerelease": True, "draft": True},
            {"tag_name": "v0.6.0", "html_url": "u06", "prerelease": False, "draft": False},
        ]

        def api(path):
            if "releases?per_page" in path:
                return rels
            raise AssertionError(f"/releases/latest must not be used for llama.cpp: {path}")

        up = rw.latest_upstream("ggml-org/llama.cpp", api)
        self.assertEqual(up.tag, "b11552")

    def test_other_upstream_uses_latest_then_tags(self):
        up = rw.latest_upstream("o/r", lambda p: {"tag_name": "v1.2.0", "html_url": "u"} if p.endswith("/releases/latest") else None)
        self.assertEqual(up.tag, "v1.2.0")
        up = rw.latest_upstream("o/r", lambda p: None if p.endswith("/releases/latest") else [{"name": "v1.0"}, {"name": "v1.10"}, {"name": "v1.9"}])
        self.assertEqual(up.tag, "v1.10")

    def test_rewrite_source_table_touches_only_source(self):
        text = MANIFEST.format(version="b11510", ref=OLD_SHA)
        new, n = rw.rewrite_source_table(text, "b11552", NEW_SHA, "llama.cpp release b11552")
        self.assertEqual(n, 3)
        doc = tomllib.loads(new)
        self.assertEqual(doc["source"]["version"], "b11552")
        self.assertEqual(doc["source"]["ref"], NEW_SHA)
        self.assertEqual(doc["lifecycle"]["ref"], "must-not-change")
        self.assertIn("# tag prefix", new)  # trailing comment kept

    def test_rewrite_containerfile_args_only_matching_args(self):
        cf = CONTAINERFILE.format(version="b11510", ref=OLD_SHA)
        new, n = rw.rewrite_containerfile_args(cf, {OLD_SHA: NEW_SHA, "b11510": "b11552"})
        self.assertEqual(n, 2)
        self.assertIn("ARG LLAMA_VERSION=b11552", new)
        self.assertIn(f'ARG LLAMA_REF="{NEW_SHA}"', new)
        self.assertIn("ARG KEEP=other", new)
        self.assertIn(f"RUN echo {OLD_SHA}", new)  # not an ARG line: untouched

    def _api(self, latest="b11552"):
        def api(path):
            if "releases?per_page" in path:
                return [{"tag_name": latest, "html_url": "https://x/rel", "prerelease": True, "draft": False}]
            if "/commits/" in path:
                return {"sha": NEW_SHA}
            if path.endswith("/releases/latest"):
                return {"tag_name": "v9.9.9", "html_url": "https://x/other"}
            raise AssertionError(path)
        return api

    def test_bump_edits_both_runners_together_and_reports_the_rest(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_tree(root, {
                "standard": {"version": "b11510"},
                "cpu": {"version": "b11500", "ref": "b11500"},
                "flm": {"manifest": MANIFEST.replace("ggml-org/llama.cpp", "FastFlowLM/FastFlowLM").format(version="0.9.40", ref="v0.9.40")},
            })
            (root / "runners" / "cpu" / "Containerfile").write_text(CONTAINERFILE.format(version="b11500", ref="b11500"))
            rows, bump, report = rw.run(root, self._api(), apply=True)

            self.assertEqual(bump.runners, ["cpu", "standard"])
            self.assertEqual(bump.previous, {"cpu": "b11500", "standard": "b11510"})
            for key in ("standard", "cpu"):
                src = tomllib.loads((root / "runners" / key / "manifest.toml").read_text())["source"]
                self.assertEqual(src["version"], "b11552")
            # SHA pins stay SHAs, tag pins stay tags.
            self.assertEqual(tomllib.loads((root / "runners/standard/manifest.toml").read_text())["source"]["ref"], NEW_SHA)
            self.assertEqual(tomllib.loads((root / "runners/cpu/manifest.toml").read_text())["source"]["ref"], "b11552")
            self.assertIn(f'ARG LLAMA_REF="{NEW_SHA}"', (root / "runners/standard/Containerfile").read_text())
            self.assertIn("ARG LLAMA_REF=\"b11552\"", (root / "runners/cpu/Containerfile").read_text())
            # flm is report-only: untouched, listed as newer.
            self.assertIn('version = "0.9.40"', (root / "runners/flm/manifest.toml").read_text())
            flm = next(r for r in rows if r.key == "flm")
            self.assertEqual((flm.status, flm.latest), ("newer", "v9.9.9"))
            self.assertIn("report only", report)

    def test_no_bump_when_current_and_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_tree(root, {"standard": {"version": "b11552"}})
            _, bump, _ = rw.run(root, self._api(), apply=True)
            self.assertIsNone(bump)
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_tree(root, {"standard": {"version": "b11510"}})
            before = (root / "runners/standard/manifest.toml").read_text()
            _, bump, _ = rw.run(root, self._api(), apply=False)
            self.assertEqual(bump.tag, "b11552")
            self.assertEqual((root / "runners/standard/manifest.toml").read_text(), before)

    def test_bump_fails_loudly_without_an_arg_to_edit(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_tree(root, {"standard": {"containerfile": "FROM scratch\nRUN git checkout b11510\n"}})
            with self.assertRaises(SystemExit):
                rw.run(root, self._api(), apply=True)


class PromoteTests(unittest.TestCase):
    RESULT = {"runner": "standard", "image": "ghcr.io/hal0ai/hal0-runner-standard",
              "tag": "b11552-r1a2b3c4", "digest": DIGEST}

    def test_edit_catalogue_v1_and_v2_shapes_touch_only_tag_and_digest(self):
        for list_key, id_key in (("images", "id"), ("runners", "key")):
            raw = json.dumps({"schema": "x", list_key: [
                {id_key: "standard", "image": self.RESULT["image"], "tag": "old", "digest": "sha256:" + "0" * 64, "summary": "é"},
                {id_key: "cpu", "image": "ghcr.io/hal0ai/hal0-runner-cpu", "tag": "t"},
            ]}, indent=2, ensure_ascii=False) + "\n"
            out = json.loads(promote.edit_catalogue(raw, self.RESULT))
            std, cpu = out[list_key]
            self.assertEqual((std["tag"], std["digest"]), (self.RESULT["tag"], DIGEST))
            self.assertEqual(cpu["tag"], "t")
            self.assertEqual(std["summary"], "é")
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
        raw = json.dumps({"version": 1, promote.HAL0_PIN_TABLE: {
            "standard": {"image": "old", "tag": "old", "digest": "old", "_notes": "keep me"},
            "cpu": {"image": "i", "tag": "t", "digest": "d"},
        }}, indent=2) + "\n"
        out = json.loads(promote.edit_hal0_pins(raw, self.RESULT))
        row = out[promote.HAL0_PIN_TABLE]["standard"]
        self.assertEqual(row[promote.HAL0_FIELDS["digest"]], DIGEST)
        self.assertEqual(row[promote.HAL0_FIELDS["image"]], self.RESULT["image"])
        self.assertEqual(row["_notes"], "keep me")
        self.assertEqual(out[promote.HAL0_PIN_TABLE]["cpu"]["tag"], "t")

    def test_hal0_pin_edit_fails_closed(self):
        raw = json.dumps({promote.HAL0_PIN_TABLE: {}})
        with self.assertRaises(SystemExit):
            promote.edit_hal0_pins(raw, self.RESULT)
        created = json.loads(promote.edit_hal0_pins(raw, self.RESULT, create_missing=True))
        self.assertIn("standard", created[promote.HAL0_PIN_TABLE])
        with self.assertRaises(SystemExit):
            promote.edit_hal0_pins(json.dumps({"other": {}}), self.RESULT)

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
