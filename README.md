# hal0-runner-images

The container images [hal0](https://github.com/Hal0ai/hal0) runs as slot
runners, and the catalogue that tells hal0 which ones exist. Every image is
built here, in CI, from a recipe in `runners/<key>/`.

## The runners

One name per runner, used everywhere: the hal0 runner key, the catalogue
`id`, the recipe directory `runners/<key>/` and the package
`ghcr.io/hal0ai/hal0-runner-<key>`.

| key | title | what it runs | upstream pin | tier |
|---|---|---|---|---|
| `standard` | Standard | llama.cpp on AMD GPUs, ROCm and Vulkan (gfx1151) | ggml-org/llama.cpp release `b11552` | default |
| `cpu` | CPU | llama.cpp on the processor, any x86_64 box | ggml-org/llama.cpp release `b11552` | default |
| `flm` | NPU (FastFlowLM) | FastFlowLM on the AMD XDNA 2 NPU | ROCm/FastFlowLM `v0.9.44` | default |
| `kokoro` | Kokoro speech | text-to-speech on the CPU | kokoro-onnx `0.4.9` | default |
| `moonshine` | Moonshine transcription | speech-to-text on the CPU | useful-moonshine-onnx `20251121` | default |
| `comfyui` | ComfyUI | image and video generation on the GPU (ROCm) | Comfy-Org/ComfyUI `0.28.0` (commit `c9602625`) | default |
| `qwen3tts` | Qwen3 TTS | multilingual text-to-speech on the GPU (ROCm) | qwen-tts `0.1.1` | optional |
| `cuda` | NVIDIA CUDA | llama.cpp on NVIDIA GPUs | ggml-org's own `server-cuda` image (not built here) | optional |

`standard` is the default for AMD GPUs: unmodified upstream llama.cpp at a
tagged release with one small patch, ROCm 10.0 and Vulkan (RADV) in one
image. `cpu` uses the same llama.cpp release.

## Tags

Every build gets one tag that never moves: `<upstream version>-r<recipe sha7>`,
for example `hal0-runner-standard:b11552-r1a2b3c4` or
`hal0-runner-flm:0.9.44-r1a2b3c4`. `<recipe sha7>` is the last commit that
changed `runners/<key>/`, so an unchanged recipe always has the same tag and a
pushed tag is never pushed again. There are no `latest`, `main` or `v1` tags.
hal0 pins `image:tag@digest`; the digest is what counts.

## Layout

```
runners/<key>/manifest.toml   every pin for one runner: base images by digest, upstream source,
                              build arguments, patches, the hardware gate, why it exists
runners/<key>/Containerfile   the image definition (no pin defaults; values come from the manifest)
runners/<key>/...             patches/, the entrypoint, server code, requirements.txt
images.json                   the catalogue hal0 reads (docs/SCHEMA.md)
retention-allowlist.json      old packages the GHCR cleanup must keep for installed boxes
scripts/runner.py             builds any runner: list, tag, build, build --check
scripts/source_watch.py       weekly report: which upstreams have a newer release
scripts/retention.py          GHCR cleanup (dry run unless asked to delete)
scripts/test_*.py             stdlib checks, run on every pull request
docs/UPDATING.md              how to update, gate and publish a runner
docs/SCHEMA.md                the images.json format
docs/HAL0.md                  how hal0 uses these images
```

## Building locally

```
python3 scripts/runner.py list                      # every runner, its image and next tag
python3 scripts/runner.py build standard --check    # clone the source, apply patches, build nothing
python3 scripts/runner.py build kokoro              # build with docker or podman (no push)
```

The checks need only Python 3.11+ and git:

```
python3 scripts/test_repo_consistency.py
python3 scripts/test_retention_fixtures.py
python3 scripts/test_source_watch.py
```

To change a runner, follow [docs/UPDATING.md](docs/UPDATING.md).
