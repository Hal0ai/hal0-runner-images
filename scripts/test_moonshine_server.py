#!/usr/bin/env python3
"""Tests for runners/moonshine/moonshine_server.py. No network, no weights:
fastapi, numpy, soundfile and moonshine_onnx are stubbed, so only the model
load and reporting logic runs.

    python3 scripts/test_moonshine_server.py
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "runners" / "moonshine" / "moonshine_server.py"


class _Anything:
    """Stand-in for fastapi names: callable, and a decorator factory."""

    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self if not (len(a) == 1 and callable(a[0])) else a[0]

    def __getattr__(self, name):
        return _Anything()


def _stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class FakeModel:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def load_server():
    for name in ("numpy", "soundfile"):
        _stub(name)
    _stub(
        "fastapi",
        FastAPI=_Anything,
        File=_Anything(),
        Form=_Anything(),
        HTTPException=Exception,
        UploadFile=object,
        WebSocket=object,
    )
    _stub("fastapi.responses", JSONResponse=object)
    _stub("moonshine_onnx", MoonshineOnnxModel=FakeModel)
    spec = importlib.util.spec_from_file_location("moonshine_server_under_test", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ms = load_server()


def touch(d: Path, *names: str) -> Path:
    for n in names:
        (d / n).write_bytes(b"")
    return d


class LoadedModelReporting(unittest.TestCase):
    def setUp(self):
        for k in ms._state:
            ms._state[k] = None
        ms._state["loaded"] = False
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_streaming_arch_reports_the_base_weights(self):
        touch(self.dir, "encoder_model.ort", "decoder_model_merged.ort")
        ms._load_model("small_streaming", str(self.dir))
        self.assertEqual(ms._state["model_id"], "moonshine-base-en")
        self.assertEqual(ms._state["model_arch"], "small_streaming")
        self.assertEqual(ms._state["model"].kwargs["model_name"], "moonshine/base")
        self.assertTrue(ms._state["loaded"])

    def test_every_arch_reports_the_weights_it_maps_to(self):
        want = {
            "tiny": "moonshine-tiny-en",
            "tiny_streaming": "moonshine-tiny-en",
            "base": "moonshine-base-en",
            "small": "moonshine-base-en",
            "small_streaming": "moonshine-base-en",
        }
        self.assertEqual(set(want), set(ms._ARCH_TO_HF_NAME))
        touch(self.dir, "encoder_model.onnx", "decoder_model_merged.onnx")
        for arch, model_id in want.items():
            with self.subTest(arch=arch):
                ms._load_model(arch, str(self.dir))
                self.assertEqual(ms._state["model_id"], model_id)

    def test_alias_is_logged(self):
        touch(self.dir, "encoder_model.ort")
        with self.assertLogs("moonshine-server", level="WARNING") as cm:
            ms._load_model("small_streaming", str(self.dir))
        self.assertIn("moonshine-base-en", "\n".join(cm.output))

    def test_matching_arch_does_not_warn(self):
        touch(self.dir, "encoder_model.ort")
        with self.assertNoLogs("moonshine-server", level="WARNING"):
            ms._load_model("base", str(self.dir))

    def test_health_and_models_report_loaded_id(self):
        import asyncio

        touch(self.dir, "encoder_model.ort")
        ms._load_model("small_streaming", str(self.dir))
        self.assertEqual(asyncio.run(ms.health())["model_id"], "moonshine-base-en")
        self.assertEqual(asyncio.run(ms.models())["data"][0]["id"], "moonshine-base-en")


class StreamingLayoutRejected(unittest.TestCase):
    def setUp(self):
        for k in ms._state:
            ms._state[k] = None
        ms._state["loaded"] = False
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_streaming_dir_is_rejected_with_named_layout(self):
        touch(self.dir, "frontend.ort", "encoder.ort", "adapter.ort", "decoder_kv.ort")
        with self.assertRaises(ms.UnsupportedModelLayout) as cm:
            ms._load_model("small_streaming", str(self.dir))
        msg = str(cm.exception)
        self.assertIn("streaming layout", msg)
        self.assertIn("encoder.ort", msg)
        self.assertIn("encoder_model.ort", msg)
        self.assertIn("decoder_model_merged.ort", msg)
        self.assertIn("tiny-en", msg)
        self.assertIn("base-en", msg)
        self.assertFalse(ms._state["loaded"])
        self.assertIsNone(ms._state["model_id"])

    def test_supported_layout_wins_over_stray_streaming_names(self):
        touch(self.dir, "encoder_model.ort", "decoder_model_merged.ort", "encoder.ort")
        ms._load_model("base", str(self.dir))
        self.assertTrue(ms._state["loaded"])

    def test_empty_dir_still_falls_back_to_hf(self):
        with self.assertLogs("moonshine-server", level="WARNING"):
            ms._load_model("tiny", str(self.dir))
        self.assertEqual(ms._state["model_id"], "moonshine-tiny-en")
        self.assertEqual(ms._state["model_path"], "moonshine/tiny")

    def test_detect_streaming_layout(self):
        self.assertEqual(ms._detect_streaming_layout(self.dir), [])
        touch(self.dir, "frontend.onnx", "cross_kv.ort")
        self.assertEqual(ms._detect_streaming_layout(self.dir), ["cross_kv.ort", "frontend.onnx"])


if __name__ == "__main__":
    unittest.main()
