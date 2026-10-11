#!/usr/bin/env python3
"""Tests for scripts/gate.py. No hardware, no podman, no network: the host,
podman and the servers are fakes. Report checks go through ci/promote.py's own
validator, so the gate and promote cannot drift apart.

    python3 scripts/test_gate.py
"""

from __future__ import annotations

import io
import json
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate  # noqa: E402
import runner  # noqa: E402
from gate import promote  # noqa: E402

D1 = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64
GIB = gate.GIB


def result(name, lane, status="passed", info=False):
    return gate.Result(name, lane, status, f"{name} detail", info)


def report(results, lanes=("rocm", "vulkan"), allow=(), key="standard", digest=D1):
    return gate.build_report(key=key, image=f"ghcr.io/hal0ai/hal0-runner-{key}", digest=digest,
                             tag="b11552-r1234567", results=results, lanes=list(lanes), allow_skip=list(allow),
                             date="2026-10-12T09:00:00Z", models=[], revision="0" * 40)


def promote_accepts(doc, key="standard", digest=D1):
    try:
        promote.validate_gate_report(promote.report_for(doc, key), key, digest)
        return True
    except SystemExit:
        return False


def wav_bytes(seconds=1.0, rate=16000, amp=0.5, channels=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(seconds * rate)
        w.writeframes(b"".join(struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * 440 * i / rate)))
                               * channels for i in range(n)))
    return buf.getvalue()


class ManifestTests(unittest.TestCase):
    def test_every_runner_has_a_valid_gate(self):
        for key in runner.keys():
            doc, ver = gate.load_plan(key)
            self.assertEqual(gate.validate_gate(ver, doc["runner"]["backends"]), [], key)
            self.assertNotIn("probe", doc.get("verification", {}))  # probes live in gates/, not in the recipe

    def test_validation_catches_mistakes(self):
        doc, ver = gate.load_plan("cpu")
        ver["colour"] = 1
        ver["lanes"]["rocm"] = {"devices": ["kfd"], "colour": []}
        ver["probe"].append({"name": "version", "doc": "dup", "kind": "exec"})
        ver["probe"].append({"name": "x", "doc": "d", "kind": "nope"})
        ver["session"][0]["check"].append({"name": "y", "doc": "d", "kind": "completion"})
        ver["session"][0]["check"].append({"name": "z", "doc": "d", "kind": "mtp", "role": "sideways"})
        problems = "\n".join(gate.validate_gate(ver, doc["runner"]["backends"]))
        for want in ("unknown top-level key 'colour'", "lane 'rocm' is not one of", "unknown key 'colour'",
                     "not under /dev", "used twice",
                     "kind 'nope'", "completion needs ['expect', 'prompt']", "role must be"):
            self.assertIn(want, problems)

    def test_lanes_are_backends_and_probe_kinds_cover_every_check(self):
        self.assertEqual(set(gate.CHECKS), set(gate.CHECK_KINDS))
        for key in runner.keys():
            doc, ver = gate.load_plan(key)
            self.assertTrue(set(ver["lanes"]) <= set(doc["runner"]["backends"]), key)

    def test_group_is_every_runner_of_the_shared_pin(self):
        self.assertEqual(gate.group_keys("llama.cpp"), ["cpu", "standard"])
        with self.assertRaises(SystemExit):
            gate.group_keys("nothing")

    def test_placeholders(self):
        self.assertEqual(gate.path_keys(["-m", "{path:qwen3.8-27b}", "--mmproj", "{path:a-b}", "{path:a-b}"]),
                         ["qwen3.8-27b", "a-b"])
        seen = []

        def res(kind, key, field):
            seen.append((kind, key, field))
            return {"path": "/m/x.gguf", "value": "llama3.2:1b", "version": "b11552"}.get(kind, "?")
        self.assertEqual(gate.render("{path:k} {value:flm-models:tag} {version}", res), "/m/x.gguf llama3.2:1b b11552")
        self.assertEqual(gate.render("v{version}", res, regex=True), r"vb11552")
        self.assertIn(("value", "flm-models", "tag"), seen)


class ReportTests(unittest.TestCase):
    def test_all_passed_is_accepted_by_promote_with_both_lanes(self):
        doc = report([result("image", None), result("version", "rocm"), result("paris", "rocm"),
                      result("paris", "vulkan")])
        self.assertTrue(doc["passed"])
        self.assertTrue(promote_accepts(doc))
        self.assertEqual(promote.validate_gate_report(doc, "standard", D1)[1], ["rocm", "vulkan"])
        self.assertEqual([p["name"] for p in doc["probes"]], ["image", "rocm/version", "rocm/paris", "vulkan/paris"])
        gate.self_check(doc, {"standard": D1})

    def test_a_failed_probe_fails_the_report_and_its_lane(self):
        doc = report([result("paris", "rocm"), result("paris", "vulkan", "failed")])
        self.assertFalse(doc["passed"])
        self.assertEqual(doc["lanes"], {"rocm": {"passed": True, "probes": 1}, "vulkan": {"passed": False, "probes": 1}})
        self.assertFalse(promote_accepts(doc))
        gate.self_check(doc, {"standard": D1})  # promote refuses it, as it should

    def test_a_skip_fails_unless_allowed_and_allowed_skips_are_listed(self):
        res = [result("paris", "rocm"), result("brain-sft-tool-call", "rocm", "skipped"),
               result("paris", "vulkan")]
        doc = report(res)
        self.assertFalse(doc["passed"])
        self.assertEqual([p for p in doc["probes"] if p.get("skipped")][0]["name"], "rocm/brain-sft-tool-call")
        self.assertFalse(promote_accepts(doc))
        for allow in (["rocm/brain-sft-tool-call"], ["brain-sft-*"], ["*/brain-sft-tool-call"]):
            doc = report(res, allow=allow)
            self.assertTrue(doc["passed"], allow)
            self.assertEqual(doc["skipped"], [{"name": "rocm/brain-sft-tool-call",
                                               "reason": "brain-sft-tool-call detail"}])
            self.assertTrue(promote_accepts(doc))

    def test_a_lane_whose_probes_were_all_skipped_is_not_gated(self):
        doc = report([result("paris", "rocm"), result("paris", "vulkan", "skipped")], allow=["vulkan/*"])
        self.assertTrue(doc["passed"])
        self.assertEqual(promote.validate_gate_report(doc, "standard", D1)[1], ["rocm"])
        nothing = report([result("paris", "rocm", "skipped")], lanes=("rocm",), allow=["*"])
        self.assertFalse(nothing["passed"])
        self.assertEqual(nothing["probes"], [])

    def test_informational_results_never_gate(self):
        doc = report([result("paris", "rocm"), result("paris", "vulkan"),
                      result("structured-reasoning-none", "rocm", "failed", info=True)])
        self.assertTrue(doc["passed"])
        self.assertEqual(doc["informational"][0]["status"], "failed")
        self.assertTrue(promote_accepts(doc))

    def test_group_document_holds_one_entry_per_runner(self):
        std = report([result("paris", "rocm"), result("paris", "vulkan")])
        cpu = report([result("paris", "cpu")], lanes=("cpu",), key="cpu", digest=D2)
        doc = gate.group_report([std, cpu], std["date"])
        self.assertEqual(doc["schema"], gate.SCHEMA)
        self.assertTrue(all("schema" not in r for r in doc["runners"]))
        self.assertTrue(promote_accepts(doc, "standard", D1))
        self.assertTrue(promote_accepts(doc, "cpu", D2))
        self.assertFalse(promote_accepts(doc, "cpu", D1))
        gate.self_check(doc, {"standard": D1, "cpu": D2})

    def test_self_check_catches_a_report_promote_would_misread(self):
        doc = report([result("paris", "rocm"), result("paris", "vulkan")])
        doc["digest"] = D2  # says passed, but promote would refuse it for D1
        with self.assertRaises(SystemExit):
            gate.self_check(doc, {"standard": D1})

    def test_scrub_removes_paths_hosts_and_private_addresses(self):
        lan_a, lan_b = ".".join(["10", "0", "1", "142"]), ".".join(["192", "168", "1", "5"])  # no literal LAN address in the repo
        doc = {"probes": [{"detail": f"open /srv/models/q.gguf failed on strix-box.lan via {lan_a} and "
                                     f"{lan_b}; served on 127.0.0.1:8080, ROCm 10.0.0-4"}],
               "lanes": {"rocm": {"passed": True}}}
        out = json.dumps(gate.scrub(doc, ["/srv/models", "/srv/models/q.gguf"], ["strix-box.lan", "strix-box"]))
        for leak in ("/srv/models", "strix-box", lan_a, lan_b):
            self.assertNotIn(leak, out)
        self.assertIn("127.0.0.1:8080", out)
        self.assertIn("10.0.0-4", out)
        self.assertIn("hal0", json.dumps(gate.scrub({"a": "hal0-runner"}, [], ["hal0"])))

    def test_detail_is_clipped(self):
        doc = report([gate.Result("paris", "rocm", "passed", "x" * 5000)], lanes=("rocm",))
        self.assertLessEqual(len(doc["probes"][0]["detail"]), gate.DETAIL_MAX)


class HostTests(unittest.TestCase):
    NODE = ("Node 0 MemTotal:       131072000 kB\nNode 0 MemFree:         20971520 kB\n"
            "Node 0 Active(file):     4194304 kB\nNode 0 Inactive(file):   4194304 kB\n"
            "Node 0 Shmem:            1048576 kB\nNode 0 SReclaimable:     1048576 kB\n"
            "Node 0 HugePages_Total:     0\n")
    PROC = "MemTotal:       134217728 kB\nMemAvailable:   67108864 kB\n"

    def test_meminfo(self):
        info = gate.parse_meminfo(self.NODE)
        self.assertEqual(info["MemFree"], 20 * GIB)
        self.assertEqual(info["HugePages_Total"], 0)
        self.assertEqual(gate.node_available(info), 29 * GIB)
        self.assertEqual(gate.available_memory(self.NODE, self.PROC), (29 * GIB, "node0"))
        small = "MemAvailable:   1048576 kB\n"
        self.assertEqual(gate.available_memory(self.NODE, small), (1 * GIB, "meminfo"))
        self.assertEqual(gate.available_memory(None, self.PROC), (64 * GIB, "meminfo"))
        with self.assertRaises(SystemExit):
            gate.available_memory(None, None)

    def test_headroom(self):
        ok, detail = gate.headroom_decision(40 * GIB, 30 * GIB, 8 * GIB)
        self.assertTrue(ok)
        self.assertIn("leaving 10.0 GiB", detail)
        self.assertFalse(gate.headroom_decision(40 * GIB, 33 * GIB, 8 * GIB)[0])

    def test_podman_argv_has_slot_flags_loopback_and_read_only_models(self):
        argv = gate.podman_run_argv(podman="podman", name="gate-standard-1-1", image="img@" + D1, args=["-m", "/x"],
                                    devices=["/dev/kfd", "/dev/dri/renderD128"], groups=["--group-add", "keep-groups"],
                                    podman_args=["--ipc=host"], mounts=[("/srv/m.gguf", "/gate/models/m/m.gguf", True),
                                                                       ("/tmp/c", "/cache", False)],
                                    publish=(40000, 8080), detach=True)
        s = " ".join(argv)
        for want in ("--name gate-standard-1-1", "--pull=never", "--device /dev/kfd", "--group-add keep-groups",
                     "-v /srv/m.gguf:/gate/models/m/m.gguf:ro", "-v /tmp/c:/cache ", "-p 127.0.0.1:40000:8080",
                     "--security-opt seccomp=unconfined", "--security-opt label=disable", "-d"):
            self.assertIn(want, s)
        self.assertEqual(argv[-3:], ["img@" + D1, "-m", "/x"])
        with self.assertRaises(gate.Skip):
            gate.podman_run_argv(podman="p", name="n", image="i", args=[], mounts=[("/a:b", "/t", True)])

    def test_groups_and_devices(self):
        self.assertEqual(gate.group_args(1000, {"render": 993}), ["--group-add", "keep-groups"])
        self.assertEqual(gate.group_args(0, {"video": 44, "render": 993}),
                         ["--group-add", "44", "--group-add", "993"])
        nodes = {"/dev/dri/card1", "/dev/dri/renderD128", "/dev/kfd"}
        present, missing = gate.expand_devices(
            ["/dev/kfd", "/dev/dri", "/dev/accel/accel0"],
            globber=lambda pat: sorted(n for n in nodes if n.startswith(pat.rstrip("*"))),
            exists=lambda p: p in nodes, isdir=lambda p: p == "/dev/dri")
        self.assertEqual(present, ["/dev/kfd", "/dev/dri/card1", "/dev/dri/renderD128"])
        self.assertEqual(missing, ["/dev/accel/accel0"])

    def test_models_file(self):
        text = ('root = "/srv/models"\n[models]\n"qwen3.8-27b" = "unsloth/Qwen3.8-27B-UD-Q4_K_XL.gguf"\n'
                'abs = "/opt/x.gguf"\n[models.stt-sample]\npath = "jfk.wav"\ntext = "ask not"\n')
        m = gate.Models.parse(text, "/etc")
        self.assertEqual(m.entries["qwen3.8-27b"]["path"], "/srv/models/unsloth/Qwen3.8-27B-UD-Q4_K_XL.gguf")
        self.assertEqual(m.entries["abs"]["path"], "/opt/x.gguf")
        self.assertEqual(m.value("stt-sample", "text"), "ask not")
        self.assertEqual(gate.Models.parse(text, "/etc", "/data").entries["abs"]["path"], "/opt/x.gguf")
        self.assertTrue(gate.Models.parse(text, "/etc", "/data").entries["qwen3.8-27b"]["path"].startswith("/data/"))
        with self.assertRaises(gate.Skip):
            m.path("qwen3.8-27b")  # configured, but no such file here
        with self.assertRaises(gate.Skip):
            m.path("unknown")
        with self.assertRaises(SystemExit):
            gate.Models.parse("[model]\n", "/etc")

    def test_digest_and_tag_arguments(self):
        self.assertEqual(gate.parse_pairs([D1], ["cpu"], "digest", True), {"cpu": D1})
        self.assertEqual(gate.parse_pairs([f"standard={D1},cpu={D2}"], ["cpu", "standard"], "digest", True),
                         {"standard": D1, "cpu": D2})
        for bad in ([D1], [f"cuda={D1}"], [f"cpu={D1}", f"cpu={D2}"]):
            with self.assertRaises(SystemExit):
                gate.parse_pairs(bad, ["cpu", "standard"], "digest", True)
        with self.assertRaises(SystemExit):
            gate.parse_pairs([f"cpu={D1}"], ["cpu", "standard"], "digest", True)


class ParserTests(unittest.TestCase):
    def test_wav(self):
        seconds, rms = gate.wav_stats(wav_bytes(1.5, amp=0.5))
        self.assertAlmostEqual(seconds, 1.5, places=3)
        self.assertAlmostEqual(rms, 0.5 / math.sqrt(2), places=2)
        silent = gate.wav_stats(wav_bytes(1.0, amp=0.0))
        self.assertEqual(silent[1], 0.0)
        floats = struct.pack("<4f", 0.5, -0.5, 0.5, -0.5)
        fmt = struct.pack("<HHIIHH", 3, 1, 4, 16, 4, 32)
        raw = (b"RIFF" + struct.pack("<I", 36 + len(floats)) + b"WAVEfmt " + struct.pack("<I", 16) + fmt
               + b"data" + struct.pack("<I", len(floats)) + floats)
        self.assertEqual(gate.wav_stats(raw), (1.0, 0.5))
        with self.assertRaises(gate.Fail):
            gate.wav_stats(b"ID3 mp3 bytes")
        self.assertEqual(len(gate.pcm16_mono_16k(wav_bytes(0.5))), 16000)
        with self.assertRaises(gate.Skip):
            gate.pcm16_mono_16k(wav_bytes(0.5, rate=22050))

    def test_text_parsers(self):
        self.assertEqual(gate.parse_ppl("[1]5.1,[2]6.0\nFinal estimate: PPL = 9.1234 +/- 0.0812\n"), 9.1234)
        self.assertIsNone(gate.parse_ppl("crashed"))
        self.assertEqual(gate.draft_stats({"draft_n": 100, "draft_n_accepted": 70}, ""), (100, 70))
        log = "slot 0 | draft acceptance rate = 0.70000 (  350 accepted /   500 generated)\n"
        self.assertEqual(gate.draft_stats({}, log), (500, 350))
        self.assertEqual(gate.draft_stats({}, "nothing"), (None, None))
        nodes = ("Import times for custom nodes:\n   0.0 seconds: /opt/ComfyUI/custom_nodes/ComfyUI_essentials\n"
                 "   0.2 seconds (IMPORT FAILED): /opt/ComfyUI/custom_nodes/ComfyUI-GGUF\n")
        self.assertEqual(gate.custom_node_status(nodes), {"ComfyUI_essentials": True, "ComfyUI-GGUF": False})
        # ComfyUI 0.28 prefixes each line with a coloured log level, even when not on a tty
        colored = ("\x1b[32m[INFO]\x1b[0m Import times for custom nodes:\n"
                   "\x1b[32m[INFO]\x1b[0m    0.0 seconds: /opt/ComfyUI/custom_nodes/websocket_image_save.py\n"
                   "\x1b[32m[INFO]\x1b[0m    0.0 seconds: /opt/ComfyUI/custom_nodes/ComfyUI-AMDGPUMonitor\n"
                   "\x1b[32m[INFO]\x1b[0m    0.0 seconds (IMPORT FAILED): /opt/ComfyUI/custom_nodes/ComfyUI-GGUF\n"
                   "\x1b[32m[INFO]\x1b[0m    0.1 seconds: /opt/ComfyUI/custom_nodes/ComfyUI_essentials\n")
        self.assertEqual(gate.custom_node_status(colored),
                         {"websocket_image_save.py": True, "ComfyUI-AMDGPUMonitor": True,
                          "ComfyUI-GGUF": False, "ComfyUI_essentials": True})
        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 1024, 768) + b"\x08\x02"
        self.assertEqual(gate.png_size(png), (1024, 768))
        self.assertAlmostEqual(gate.word_recall("Ask not what your country", "ask NOT what your county"), 0.8)

    def test_chat_parsers(self):
        call = {"choices": [{"message": {"tool_calls": [{"function": {"name": "get_weather",
                                                                     "arguments": '{"city": "Paris"}'}}]}}]}
        self.assertEqual(gate.first_tool_call(call)[:2], ("get_weather", {"city": "Paris"}))
        self.assertEqual(gate.first_tool_call({"choices": [{"message": {"content": "hi"}}]}), ("", None, ""))
        ok = {"choices": [{"message": {"content": '{"city": "Paris", "country": "France"}'}}]}
        self.assertEqual(gate.structured_problem(200, ok, "json_schema"), "")
        self.assertIn("not JSON", gate.structured_problem(200, {"choices": [{"message": {"content": "Paris"}}]},
                                                          "json_object"))
        self.assertIn("schema", gate.structured_problem(200, {"choices": [{"message": {"content": '{"a": 1}'}}]},
                                                        "json_schema"))
        err = {"error": {"message": "Failed to initialize samplers"}}
        self.assertIn("Failed to initialize samplers", gate.structured_problem(500, err, "json_schema"))

    def test_payloads(self):
        tools = gate.large_tool_array(gate.tool_count_for(29000, 250.0))
        self.assertEqual(len(tools), 135)  # ceil(29000 * 1.15 / 250) decoys + the target
        self.assertEqual(tools[-1]["function"]["name"], "lookup_capital")
        self.assertEqual(tools, gate.large_tool_array(134))  # deterministic
        unmeasured = gate.tool_count_for(29000, None)  # assumes 3 characters per token
        self.assertGreaterEqual(len(json.dumps(gate.large_tool_array(unmeasured))), 29000 * 3)
        schema = gate.grammar_schema(2500, 10)
        self.assertEqual(schema["properties"]["summary"]["minLength"], 2500)
        self.assertEqual(len(schema["properties"]), 11)
        body = gate.multipart({"response_format": "json"}, ("file", "s.wav", b"RIFF..", "audio/wav"), "BB")
        self.assertIn(b'name="file"; filename="s.wav"\r\nContent-Type: audio/wav\r\n\r\nRIFF..\r\n', body)
        self.assertTrue(body.endswith(b"--BB--\r\n"))
        wf = gate.comfy_workflow("sdxl.safetensors", 1024, 1024, 20)
        self.assertEqual(wf["1"]["inputs"]["ckpt_name"], "sdxl.safetensors")
        self.assertEqual(wf["7"]["class_type"], "SaveImage")

    def test_websocket_frames(self):
        frame = gate.ws_frame(0x2, b"hello" * 100, b"\x01\x02\x03\x04")
        opcode, payload, used = gate.ws_parse(frame)
        self.assertEqual((opcode, payload, used), (0x2, b"hello" * 100, len(frame)))
        server = bytes([0x81, 5]) + b'{"a":'
        self.assertIsNone(gate.ws_parse(server[:4]))
        text = b'{"text": "ask not", "is_final": false}'
        srv = bytes([0x81, len(text)]) + text + b"tail"
        self.assertEqual(gate.ws_parse(srv)[:2], (0x1, text))

    def test_registry_digest_asks_anonymously_for_the_tag(self):
        calls = []

        class Resp:
            def __init__(self, body=b"", headers=None):
                self.body, self.headers = body, headers or {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self.body

        def opener(req, timeout=None):
            calls.append(req)
            if "/token" in req.full_url:
                return Resp(b'{"token": "t"}')
            return Resp(headers={"Docker-Content-Digest": D1})

        self.assertEqual(gate.registry_digest("ghcr.io/hal0ai/hal0-runner-cpu", "b1-r1234567", opener), D1)
        self.assertIn("scope=repository:hal0ai/hal0-runner-cpu:pull", calls[0].full_url)
        self.assertEqual(calls[1].get_method(), "HEAD")
        self.assertTrue(calls[1].full_url.endswith("/v2/hal0ai/hal0-runner-cpu/manifests/b1-r1234567"))

    def test_upload_creates_a_prerelease_once_and_never_replaces_an_asset(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1 if argv[2] == "view" else 0, "", "")

        url = gate.upload(Path("gate-cpu-b1-r1234567-20261012T090000Z.json"), "gate-cpu-b1-r1234567", "Gate", run)
        self.assertEqual(url, "https://github.com/Hal0ai/hal0-runner-images/releases/download/"
                              "gate-cpu-b1-r1234567/gate-cpu-b1-r1234567-20261012T090000Z.json")
        self.assertEqual([c[2] for c in calls], ["view", "create", "upload"])
        self.assertIn("--prerelease", calls[1])
        self.assertIn("--latest=false", calls[1])
        self.assertNotIn("--clobber", calls[2])
        self.assertEqual(gate.release_tag("llama.cpp", "b11552"), "gate-llama.cpp-b11552")


# ---------------------------------------------------------------------------
# The whole gate against fakes
# ---------------------------------------------------------------------------

class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def _route(self, method, path, body):
        self.requests.append((method, path, body))
        fn = self.routes.get((method, path.split("?")[0]))
        return fn(body) if fn else (404, b"not found")

    def get(self, path, timeout=30):
        st, data = self._route("GET", path, None)
        return st, data if isinstance(data, bytes) else json.dumps(data).encode()

    def post_json(self, path, obj, timeout=600):
        st, data = self._route("POST", path, obj)
        if isinstance(data, bytes):
            try:
                data = json.loads(data)
            except ValueError:
                data = {"_raw": data.decode(errors="replace")}
        return st, data

    def post_raw(self, path, body, ctype, timeout=600):
        st, data = self._route("POST", path, body)
        return st, data if isinstance(data, bytes) else json.dumps(data).encode()


class FakeHost(gate.Host):
    def __init__(self, http, labels, memory=(64 * GIB,), exec_out=None, logs=""):
        super().__init__("podman", verbose=False)
        self.fake_http, self.labels, self.mem = http, labels, list(memory)
        self.exec_out = exec_out or (lambda argv: (0, ""))
        self.log_text = logs
        self.calls = []
        self.removed = []

    def podman(self, argv, timeout=None):
        self.calls.append(argv)
        sub = argv[1]
        out = (0, "")
        if sub == "image":
            out = (0, json.dumps(self.labels))
        elif sub == "logs":
            out = (0, self.log_text)
        elif sub == "inspect":
            out = (0, "true 0")
        elif sub == "rm":
            self.removed.append(argv[-1])
        elif sub == "run" and "-d" not in argv:
            out = self.exec_out(argv)
        return subprocess.CompletedProcess(argv, out[0], out[1])

    def memory(self):
        return (self.mem.pop(0) if len(self.mem) > 1 else self.mem[0]), "node0"

    def devices(self, devs):
        return list(devs), []

    def groups(self):
        return ["--group-add", "keep-groups"]

    def free_port(self):
        return 40000

    def lock(self):
        pass

    def http(self, port):
        return self.fake_http

    def sleep(self, s):
        pass

    def size(self, path):
        return 3 * GIB

    def is_dir(self, path):
        return Path(path).is_dir()


class GateRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        model = Path(self.tmp.name) / "LFM2.5-2.6B-Q8_0.gguf"
        model.write_bytes(b"GGUF")
        self.models = gate.Models({"lfm2.5-2.6b": {"path": str(model)}}, str(Path(self.tmp.name) / "g.toml"))
        self.real_registry = gate.registry_digest
        gate.registry_digest = lambda image, tag: D1
        version = runner.load("cpu")["image"]["version"]
        self.labels = {"dev.hal0.runner.key": "cpu", "org.opencontainers.image.version": f"{version}-r1234567"}
        self.version_digits = version.lstrip("b")

    def tearDown(self):
        gate.registry_digest = self.real_registry
        self.tmp.cleanup()

    def cpu_host(self, **kw):
        http = FakeHttp({("GET", "/health"): lambda b: (200, {"status": "ok"}),
                         ("POST", "/completion"): lambda b: (200, {"content": " Paris, of course."})})

        def exec_out(argv):
            if "--version" in argv:
                return 0, (f"version: 0.6.0-dev (build {self.version_digits}, commit 23b0202a1) "
                          "built with GNU 13.3.0 for Linux x86_64")
            return 0, "Available devices:\n"
        return FakeHost(http, self.labels, exec_out=exec_out, **kw)

    def run_gate(self, host, allow=()):
        g = gate.Gate("cpu", D1, None, self.models, host)
        results = g.run()
        return g, gate.build_report(key="cpu", image=g.image, digest=D1, tag=g.tag, results=results,
                                    lanes=list(g.ver["lanes"]), allow_skip=list(allow), date="d",
                                    models=list(g.used_models.values()), revision="r")

    def test_cpu_gate_passes_and_promote_accepts_it(self):
        host = self.cpu_host()
        g, doc = self.run_gate(host)
        self.assertTrue(doc["passed"], json.dumps(doc["probes"], indent=1))
        self.assertTrue(promote_accepts(doc, "cpu", D1))
        self.assertEqual(promote.validate_gate_report(doc, "cpu", D1)[1], ["cpu"])
        self.assertEqual([p["name"] for p in doc["probes"]],
                         ["image", "version", "no-gpu-devices", "cpu/health", "cpu/paris"])
        self.assertEqual(doc["informational"][0]["name"], "no-avx512-host")
        self.assertEqual(doc["models"], [{"key": "lfm2.5-2.6b", "file": "LFM2.5-2.6B-Q8_0.gguf", "bytes": 3 * GIB}])
        self.assertEqual(g.tag, self.labels["org.opencontainers.image.version"])
        runs = [c for c in host.calls if c[1] == "run"]
        self.assertTrue(all(c[3].startswith("gate-cpu-") for c in runs))
        self.assertEqual(sorted(host.removed), sorted(c[3] for c in runs))  # every container removed
        session = [c for c in runs if "-d" in c][0]
        s = " ".join(session)
        self.assertIn("-p 127.0.0.1:40000:8080", s)
        self.assertIn(f"{self.models.entries['lfm2.5-2.6b']['path']}:/gate/models/lfm2.5-2.6b/LFM2.5-2.6B-Q8_0.gguf:ro", s)
        self.assertTrue(s.endswith("--port 8080 -ngl 0"))
        self.assertNotIn("--device", s)

    def test_insufficient_memory_skips_the_load_and_fails_the_gate(self):
        host = self.cpu_host(memory=(10 * GIB,))
        g, doc = self.run_gate(host)
        self.assertFalse(doc["passed"])
        skipped = [p for p in doc["probes"] if p.get("skipped")]
        self.assertEqual([p["name"] for p in skipped], ["cpu/health", "cpu/paris"])
        self.assertIn("insufficient memory", skipped[0]["detail"])
        self.assertFalse(any("-d" in c for c in host.calls if c[1] == "run"))  # never started
        self.assertFalse(promote_accepts(doc, "cpu", D1))

    def test_a_moved_tag_stops_the_gate_before_any_container_runs(self):
        gate.registry_digest = lambda image, tag: D2
        host = self.cpu_host()
        g, doc = self.run_gate(host)
        self.assertFalse(doc["passed"])
        self.assertEqual([p["name"] for p in doc["probes"]], ["image"])
        self.assertIn("now points at", doc["probes"][0]["detail"])
        self.assertFalse([c for c in host.calls if c[1] == "run"])

    def test_a_wrong_answer_fails_its_probe(self):
        host = self.cpu_host()
        host.fake_http.routes[("POST", "/completion")] = lambda b: (200, {"content": " Lyon."})
        _, doc = self.run_gate(host)
        bad = [p for p in doc["probes"] if not p["passed"]]
        self.assertEqual([p["name"] for p in bad], ["cpu/paris"])
        self.assertIn("want 'Paris'", bad[0]["detail"])

    def test_a_crashing_server_fails_its_session(self):
        host = self.cpu_host()
        host.fake_http.routes[("GET", "/health")] = lambda b: (0, b"connection refused")
        host.podman = (lambda orig: (lambda argv, timeout=None: subprocess.CompletedProcess(argv, 0, "false 139")
                                     if argv[1] == "inspect" else orig(argv, timeout)))(host.podman)
        _, doc = self.run_gate(host)
        bad = [p for p in doc["probes"] if not p["passed"]]
        self.assertEqual([p["name"] for p in bad], ["cpu/health", "cpu/paris"])
        self.assertIn("exited (139)", bad[0]["detail"])

    def test_stale_cleanup_removes_only_containers_of_dead_runs(self):
        import os
        host = self.cpu_host()
        live, dead = f"gate-cpu-{os.getpid()}-1", "gate-cpu-999999999-1"
        orig = host.podman
        host.podman = lambda argv, timeout=None: (
            subprocess.CompletedProcess(argv, 0, f"{live}\n{dead}\ngate-cpuextra-1-1\n") if argv[1] == "ps"
            else orig(argv, timeout))
        gate.Gate("cpu", D1, None, self.models, host).remove_stale()
        self.assertEqual(host.removed, [dead])

    def test_one_gate_at_a_time(self):
        import os
        self.assertEqual(gate.LOCK_FILE, "/run/lock/hal0-gate.lock")  # never /tmp or a per-user dir
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "hal0-gate.lock")
            old = os.environ.get(gate.LOCK_ENV)
            os.environ[gate.LOCK_ENV] = path
            try:
                first, second = gate.Host(verbose=False), gate.Host(verbose=False)
                first.lock()
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
                with self.assertRaises(SystemExit):
                    second.lock()
                first.cleanup()
                second.lock()  # free again once the first run is done
                second.cleanup()
            finally:
                if old is None:
                    os.environ.pop(gate.LOCK_ENV)
                else:
                    os.environ[gate.LOCK_ENV] = old

    def test_the_lock_refuses_files_someone_else_could_replace(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            loose = Path(d) / "loose.lock"
            loose.write_text("")
            loose.chmod(0o666)  # writable by others
            with self.assertRaises(SystemExit):
                gate.open_lock(str(loose))
            target = Path(d) / "target"
            target.write_text("")
            link = Path(d) / "link.lock"
            link.symlink_to(target)  # a planted symlink is never followed
            with self.assertRaises(SystemExit):
                gate.open_lock(str(link))
            shared = Path(d) / "shared"
            shared.mkdir()
            shared.chmod(0o777)  # anyone could swap the file: refused
            with self.assertRaises(SystemExit):
                gate.open_lock(str(shared / "hal0-gate.lock"))
            shared.chmod(0o1777)  # sticky, like /tmp or Debian's /run/lock: our own file is safe
            os.close(gate.open_lock(str(shared / "hal0-gate.lock")))
        st = os.stat_result
        good_dir = st((0o40755, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        good = st((0o100644, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        self.assertEqual(gate.lock_problem(good, good_dir, 1000), "")
        self.assertIn("owned by uid 1001", gate.lock_problem(st((0o100644, 0, 0, 1, 1001, 0, 0, 0, 0, 0)), good_dir, 0))
        self.assertIn("hard links", gate.lock_problem(st((0o100644, 0, 0, 2, 0, 0, 0, 0, 0, 0)), good_dir, 0))
        self.assertIn("directory is owned", gate.lock_problem(good, st((0o40755, 0, 0, 1, 1001, 0, 0, 0, 0, 0)), 0))

    def test_the_report_is_never_written_through_a_symlink_or_over_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            gate.write_new(Path(d) / "r.json", "{}")
            with self.assertRaises(SystemExit):
                gate.write_new(Path(d) / "r.json", "{}")
            victim = Path(d) / "victim"
            victim.write_text("keep")
            (Path(d) / "planted.json").symlink_to(victim)
            with self.assertRaises(SystemExit):
                gate.write_new(Path(d) / "planted.json", "{}")
            self.assertEqual(victim.read_text(), "keep")

    def test_an_unconfigured_model_is_a_skip(self):
        self.models.entries.clear()
        _, doc = self.run_gate(self.cpu_host(), allow=["cpu/*"])
        self.assertFalse(doc["passed"])  # no lane passed
        self.assertIn("not configured in gate-models.toml", doc["skipped"][0]["reason"])

    def test_cli_writes_a_scrubbed_report_promote_accepts(self):
        host = self.cpu_host()
        with tempfile.TemporaryDirectory() as d:
            toml = Path(d) / "gate-models.toml"
            toml.write_text(f'[models]\n"lfm2.5-2.6b" = "{self.models.entries["lfm2.5-2.6b"]["path"]}"\n')
            out = Path(d) / "r.json"
            rc = gate.main(["cpu", "--digest", D1, "--models", str(toml), "--out", str(out)], host=host,
                           tree_check=lambda: [])
            text = out.read_text()
        self.assertEqual(rc, 0)
        self.assertNotIn(self.tmp.name, text)
        doc = json.loads(text)
        self.assertTrue(promote_accepts(doc, "cpu", D1))
        self.assertEqual(promote.parse_gate_reports("https://github.com/x/y/releases/download/t/r.json", ["cpu"]),
                         {"cpu": "https://github.com/x/y/releases/download/t/r.json"})


    def test_cli_refuses_a_dirty_tree_before_touching_the_host(self):
        host = self.cpu_host()
        locked = []
        host.lock = lambda: locked.append(True)
        with tempfile.TemporaryDirectory() as d:
            toml = Path(d) / "gate-models.toml"
            toml.write_text(f'[models]\n"lfm2.5-2.6b" = "{self.models.entries["lfm2.5-2.6b"]["path"]}"\n')
            out = Path(d) / "r.json"
            with self.assertRaises(gate.GateError) as cm:
                gate.main(["cpu", "--digest", D1, "--models", str(toml), "--out", str(out)], host=host,
                          tree_check=lambda: [" M gates/cpu.toml", "?? scripts/extra.py"])
            self.assertFalse(out.exists())
            # --plan runs nothing, so it is allowed on a dirty tree
            rc = gate.main(["cpu", "--plan", "--models", str(toml)], host=host,
                           tree_check=lambda: [" M gates/cpu.toml"])
        self.assertIn("gates/cpu.toml", cm.exception.msg)
        self.assertIn("scripts/extra.py", cm.exception.msg)
        self.assertEqual((locked, host.calls), ([], []))
        self.assertEqual(rc, 0)

    def test_tree_problems_lists_changes_under_the_guarded_paths(self):
        seen = []

        def run(argv, **kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, " M gates/standard.toml\n?? ci/new.py\n", "")
        self.assertEqual(gate.tree_problems(run), [" M gates/standard.toml", "?? ci/new.py"])
        self.assertEqual(seen[0][:6], ["git", "-C", str(gate.ROOT), "status", "--porcelain", "--untracked-files=all"])
        self.assertEqual(seen[0][-3:], ["gates", "scripts", "ci"])
        clean = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", "")
        self.assertEqual(gate.tree_problems(clean), [])
        broken = lambda argv, **kw: subprocess.CompletedProcess(argv, 128, "", "fatal: not a git repository")
        self.assertIn("not a git repository", gate.tree_problems(broken)[0])

        def missing(argv, **kw):
            raise FileNotFoundError("git")
        self.assertIn("git is not available", gate.tree_problems(missing)[0])

    def test_git_environment_drops_every_repository_override(self):
        env = gate.git_environment({"PATH": "/bin", "HOME": "/h", "GIT_DIR": "/x/.git", "GIT_WORK_TREE": "/x",
                                    "GIT_INDEX_FILE": "/x/i", "GIT_OBJECT_DIRECTORY": "/o",
                                    "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/a", "GIT_COMMON_DIR": "/c",
                                    "GIT_CEILING_DIRECTORIES": "/", "GIT_CONFIG_COUNT": "1",
                                    "GIT_CONFIG_KEY_0": "core.worktree", "GIT_CONFIG_VALUE_0": "/x",
                                    "GIT_CONFIG_PARAMETERS": "'core.worktree'='/x'"})
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/h", "GIT_OPTIONAL_LOCKS": "0"})

    def test_a_hostile_git_environment_cannot_vouch_for_this_checkout(self):
        """GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE pointing at a clean repository
        must not make a dirty checkout look clean or change the revision."""
        import os
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        clean_env = gate.git_environment()

        def sh(cwd, *args):
            return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                   "-c", "commit.gpgsign=false", *args], cwd=cwd, env=clean_env,
                                  capture_output=True, text=True, check=True).stdout.strip()

        with tempfile.TemporaryDirectory() as d:
            mine, other = Path(d) / "mine", Path(d) / "other"
            for repo in (mine, other):
                (repo / "gates").mkdir(parents=True)
                (repo / "gates" / "cpu.toml").write_text(repo.name)
                sh(repo, "init", "-q")
                sh(repo, "add", "-A")
                sh(repo, "commit", "-q", "-m", repo.name)
            (mine / "gates" / "cpu.toml").write_text("edited, not committed")
            want_rev = sh(mine, "rev-parse", "HEAD")
            hostile = {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                       "GIT_INDEX_FILE": str(other / ".git" / "index"),
                       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.worktree", "GIT_CONFIG_VALUE_0": str(other)}
            saved_root, saved_env = gate.ROOT, {k: os.environ.get(k) for k in hostile}
            gate.ROOT = mine
            os.environ.update(hostile)
            try:
                # unsanitised git would answer for `other`: clean, and its revision
                leaked = subprocess.run(["git", "-C", str(mine), "status", "--porcelain"],
                                        capture_output=True, text=True).stdout
                self.assertEqual(leaked.strip(), "")
                self.assertEqual(gate.tree_problems(), [" M gates/cpu.toml"])
                self.assertEqual(gate.git_revision(), want_rev)
                self.assertNotEqual(want_rev, sh(other, "rev-parse", "HEAD"))
            finally:
                gate.ROOT = saved_root
                for k, v in saved_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


class CheckTests(unittest.TestCase):
    """Session checks against canned server responses."""

    def ctx(self, routes, logs="", lane_state=None, args=(), session=None):
        g = gate.Gate.__new__(gate.Gate)
        g.headroom = 8 * GIB
        g.host = FakeHost(FakeHttp(routes), {}, memory=(40 * GIB,), logs=logs)
        g.logs = lambda name: logs
        return gate.SessionCtx(g, "rocm", session or {"port": 8080, "health": "/health"}, list(args),
                               g.host.fake_http, "c", 60 * GIB, lane_state if lane_state is not None else {})

    def test_mtp_on_then_off(self):
        state: dict = {}
        on = self.ctx({("POST", "/completion"): lambda b: (200, {"content": "essay", "timings": {
            "predicted_per_second": 13.0, "draft_n": 200, "draft_n_accepted": 150}})}, lane_state=state)
        ok, detail = gate.chk_mtp(on, {"role": "on"})
        self.assertTrue(ok, detail)
        self.assertIn("150/200", detail)
        off = self.ctx({("POST", "/completion"): lambda b: (200, {"content": "essay", "timings": {
            "predicted_per_second": 10.0}})}, lane_state=state)
        ok, detail = gate.chk_mtp(off, {"role": "off"})
        self.assertTrue(ok)
        self.assertIn("+30%", detail)
        self.assertIn("identical: yes", detail)
        inactive = self.ctx({("POST", "/completion"): lambda b: (200, {"content": "e", "timings": {}})})
        self.assertFalse(gate.chk_mtp(inactive, {"role": "on"})[0])
        with self.assertRaises(gate.Skip):  # no MTP-on result to compare with: a skip, not a failure
            gate.chk_mtp(self.ctx({("POST", "/completion"): lambda b: (200, {"content": "e"})}), {"role": "off"})

    def test_structured_runs_every_format_and_thinking_mode(self):
        seen = []

        def chat(body):
            seen.append((body["response_format"]["type"], body.get("chat_template_kwargs")))
            return 200, {"choices": [{"message": {"content": '{"city": "Paris", "country": "France"}'}}]}
        ok, detail = gate.chk_structured(self.ctx({("POST", "/v1/chat/completions"): chat}),
                                         {"formats": ["json_object", "json_schema"], "thinking": ["absent", "true", "false"]})
        self.assertTrue(ok, detail)
        self.assertEqual(len(seen), 6)
        self.assertIn(("json_schema", {"enable_thinking": False}), seen)
        self.assertIn(("json_object", None), seen)

    def test_large_tools_needs_enough_prompt_tokens_and_the_last_tool(self):
        def reply(tokens, name):
            return lambda b: (200, {"usage": {"prompt_tokens": tokens}, "choices": [{"message": {"tool_calls": [
                {"function": {"name": name, "arguments": '{"country": "France"}'}}]}}]})
        c = {"min_prompt_tokens": 29000}
        tokenize = {("POST", "/tokenize"): lambda b: (200, {"tokens": list(range(4000))})}
        ctx = self.ctx({**tokenize, ("POST", "/v1/chat/completions"): reply(31000, "lookup_capital")})
        self.assertTrue(gate.chk_large_tools(ctx, c)[0])
        sent = [b for m, p, b in ctx.client.requests if p == "/v1/chat/completions"][0]
        self.assertEqual(len(sent["tools"]), 168)  # 200 tokens per decoy measured: ceil(33350 / 200) + 1
        self.assertTrue(gate.chk_large_tools(self.ctx({("POST", "/v1/chat/completions"): reply(31000, "lookup_capital")}), c)[0])
        ok, detail = gate.chk_large_tools(self.ctx({("POST", "/v1/chat/completions"): reply(20000, "lookup_capital")}), c)
        self.assertFalse(ok)
        self.assertIn("at least 29000", detail)
        self.assertFalse(gate.chk_large_tools(self.ctx({("POST", "/v1/chat/completions"): reply(31000, "inventory_lookup_001")}), c)[0])

    def test_concurrency(self):
        answers = dict(gate.CONCURRENCY_PROMPTS)
        route = {("POST", "/completion"): lambda b: (200, {"content": f" {answers[b['prompt']]}."})}
        ok, detail = gate.chk_concurrency(self.ctx(route), {"parallel": 4})
        self.assertTrue(ok, detail)
        self.assertIn("4/4 identical", detail)

    def test_default_port_and_health_json(self):
        routes = {("GET", "/health"): lambda b: (200, {"status": "ok", "model_loaded": True, "model_id": "moonshine-small_streaming-en"})}
        self.assertTrue(gate.chk_http_json(self.ctx(routes), {"no_port_arg": True})[0])
        self.assertFalse(gate.chk_http_json(self.ctx(routes, args=["--port", "9"]), {"no_port_arg": True})[0])
        self.assertTrue(gate.chk_http_json(self.ctx(routes), {"equals": {"model_loaded": True},
                                                             "contains": {"model_id": "small_streaming"}})[0])
        self.assertFalse(gate.chk_http_json(self.ctx(routes), {"equals": {"model_loaded": False}})[0])

    def test_speech_and_logs(self):
        routes = {("POST", "/v1/audio/speech"): lambda b: (200, wav_bytes(1.0))}
        ok, detail = gate.chk_speech(self.ctx(routes), {"input": "hi", "language": "English"})
        self.assertTrue(ok, detail)
        silent = {("POST", "/v1/audio/speech"): lambda b: (200, wav_bytes(1.0, amp=0.0))}
        self.assertFalse(gate.chk_speech(self.ctx(silent), {"input": "hi"})[0])
        logs = "AMD arch: gfx1151\nTotal VRAM 98304 MB, total RAM 128000 MB\nSet vram state to: NORMAL_VRAM\n"
        c = {"expect": ["gfx1151", "NORMAL_VRAM"], "at_least": [{"pattern": r"Total VRAM (\d+) MB", "min": 65536}]}
        self.assertTrue(gate.chk_log(self.ctx({}, logs=logs), c)[0])
        self.assertFalse(gate.chk_log(self.ctx({}, logs=logs.replace("98304", "16384")), c)[0])

    def test_resident_memory_floor(self):
        ok, detail = gate.chk_resident_memory(self.ctx({}), {})
        self.assertTrue(ok)
        self.assertIn("took about 20.0 GiB", detail)


if __name__ == "__main__":
    unittest.main(verbosity=1)
