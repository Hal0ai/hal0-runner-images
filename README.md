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
| `flm` | NPU (FastFlowLM) | FastFlowLM on the AMD XDNA 2 NPU | ROCm/FastFlowLM `v1.0.10` | default |
| `kokoro` | Kokoro speech | text-to-speech on the CPU | kokoro-onnx `0.4.9` | default |
| `moonshine` | Moonshine transcription | speech-to-text on the CPU | useful-moonshine-onnx `20251121` | default |
| `comfyui` | ComfyUI | image and video generation on the GPU (ROCm) | Comfy-Org/ComfyUI `0.28.0` (commit `c9602625`) | default |
| `qwen3tts` | Qwen3 TTS | multilingual text-to-speech on the GPU (ROCm) | qwen-tts `0.1.1` | optional |
| `cuda` | NVIDIA CUDA | llama.cpp on NVIDIA GPUs | ggml-org's own image `server-cuda-b11541` (not built here) | optional |
| `halogen` | halogen (Flash-Next) | Peonist's engine for Qwen3.8-Flash-Next on Strix Halo (ROCm) | Peonist's own image `halogen-flash-server` `0.18.22` (not built here) | optional |

`standard` is the default for AMD GPUs: unmodified upstream llama.cpp at a
tagged release with one small patch, ROCm 10.0 and Vulkan (RADV) in one
image. `standard` and `cpu` share one llama.cpp release, pinned in
`runners/llama.cpp.toml`, so one change moves both. `cuda` is ggml-org's own
CUDA image; ggml-org does not publish one for every release, so `cuda` pins
the newest `server-cuda-b<N>` that is not newer than that release.

## Tags

Every build gets one tag that never moves: `<upstream version>-r<recipe sha7>`,
for example `hal0-runner-standard:b11552-r1a2b3c4` or
`hal0-runner-flm:0.9.44-r1a2b3c4`. `<recipe sha7>` is the last commit that
changed `runners/<key>/` (or the shared pin file it uses), so an unchanged recipe always has the same tag and a
pushed tag is never pushed again. There are no `latest`, `main` or `v1` tags.
hal0 pins `image:tag@digest`; the digest is what counts.

## Layout

```
runners/<key>/manifest.toml   every pin for one runner: base images by digest, upstream source,
                              build arguments, patches, why it exists
runners/<key>/Containerfile   the image definition (no pin defaults; values come from the manifest)
runners/<key>/...             patches/, the entrypoint, server code, requirements.txt
runners/llama.cpp.toml        the llama.cpp release shared by standard, cpu and cuda
gates/<key>.toml              the hardware gate's probes per runner (not part of the image tag)
images.json                   the catalogue hal0 reads (docs/SCHEMA.md)
retention-allowlist.json      old packages the GHCR cleanup must keep for installed boxes
scripts/runner.py             builds any runner: list, tag, build, build --check
scripts/gate.py               the hardware gate: runs gates/<key>.toml against a published
                              digest on the Strix Halo box, writes the gate report
scripts/source_watch.py       weekly report: which upstreams have a newer release
scripts/retention.py          GHCR cleanup (dry run unless asked to delete)
scripts/test_*.py             stdlib checks, run on every pull request
docs/UPDATING.md              how a runner is updated, built, gated and pinned (the CI flow)
docs/RECIPES.md               what goes in a recipe and how to change one
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
python3 scripts/test_gate.py
```

To change a runner, see [docs/RECIPES.md](docs/RECIPES.md) for the recipe and
[docs/UPDATING.md](docs/UPDATING.md) for the build, gate and pin flow.
