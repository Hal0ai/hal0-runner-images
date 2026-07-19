# hal0-comfyui — ComfyUI runner (AMD Strix Halo / gfx1151, ROCm)

Replaces the third-party `docker.io/kyuz0/amd-strix-halo-comfyui` that hal0
currently digest-pins. hal0-owned, digest-pinnable, license-clean.

## Decision (2026-07-19): single ROCm image, no backend split

The O22 lane assumed a `comfyui-rocm` vs `comfyui-vulkan` split. **It is not
warranted.** ComfyUI is PyTorch-native and PyTorch has no viable Vulkan
compute backend on gfx1151. Every maintained Strix-Halo ComfyUI image is
ROCm. The only Vulkan diffusion path is `stable-diffusion.cpp` (GGML) — a
different runtime with no node graph, custom nodes, or Wan2.2/video pipelines,
which would break hal0's existing workflows. **Build one ROCm image.**

Corollary for the app: the `comfyui` runner's `backend=None` in
`RUNNER_IMAGES` (src/hal0/runners/__init__.py) is CORRECT and needs no
`runner_for_backend`-style split. See docs/comfyui-research-2026-07-19.md and
../docs/WIRING.md.

## Provenance

- **Pattern** borrowed from `hec-ovi/comfyui-strix-docker` (MIT build glue):
  AMD TheRock nightly index, ABI-matched pinned torch triple, HSA self-pin +
  SDMA/SVM stability envs, build-time non-ROCm-torch assertion.
- **Layout** (`/opt/ComfyUI`, `/opt/venv`) mirrors the incumbent kyuz0 image
  so hal0 provider/installer paths are unchanged.
- Redistributing an image built from ComfyUI is a GPL-3.0 derivative work —
  publish under that license.

## Reproducibility status

All build inputs are pinned (2026-07-19): the fedora base by digest, the
PyTorch triple by dated nightly, and ComfyUI + all three custom nodes by
commit SHA (see `versions.env`). No floating tags remain. Before a release,
re-resolve to current SHAs if a newer ComfyUI/node/base is wanted — the pin is
the baseline, not a freeze.

Remaining pre-publish work: build the image on a host and smoke-test
(`assert 'rocm' in torch.__version__` + a `/system_stats` hit) before pushing
`:latest` and pinning its digest in the app manifest.

## Build

```
set -a; . ./versions.env; set +a
podman build -t ghcr.io/hal0ai/hal0-comfyui:dev \
  --build-arg ROCM_INDEX --build-arg TORCH_VER --build-arg VISION_VER --build-arg AUDIO_VER \
  --build-arg COMFYUI_REF --build-arg ESSENTIALS_REF --build-arg AMDGPUMONITOR_REF --build-arg GGUF_REF \
  .
```
