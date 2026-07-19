# ComfyUI on AMD Strix Halo (gfx1151) — image survey & decision

**Date:** 2026-07-19 · **Lane:** O22 (build-an-image) · Sources: 30 fetched/searched.

## TL;DR

- **kyuz0's ComfyUI image is ROCm-only, not Vulkan** (fedora:rawhide + AMD
  gfx1151 nightly PyTorch wheels). The brief's "kyuz0 ships Vulkan" was a
  cross-wire with kyuz0's *separate* llama.cpp toolboxes repo.
- **ComfyUI is PyTorch-native; PyTorch has no viable Vulkan compute backend.**
  The only Vulkan diffusion path on Strix Halo is `stable-diffusion.cpp`
  (GGML) — a different runtime, no node graph / custom nodes / video pipelines.
- **⇒ Single ROCm image. The O22 backend split is NOT warranted for ComfyUI.**
  The `comfyui` runner's `backend=None` is correct.
- **"ciru" is `ciru-ai/ROCmFPX`** — a llama.cpp FPX upstream, not a ComfyUI
  fork.
- **Recommendation:** hal0-owned single ROCm image `ghcr.io/hal0ai/hal0-comfyui`,
  forking the *pattern* of `hec-ovi/comfyui-strix-docker`, keeping kyuz0's
  `/opt/ComfyUI` + `/opt/venv` layout. Draft in `../comfyui/Dockerfile`.

## Comparison

| Image | Repo | Backend | Torch/ROCm | gfx1151 | Nodes | Size | Licence | Maint. | Pinnable |
|---|---|---|---|---|---|---|---|---|---|
| **kyuz0/amd-strix-halo-comfyui** (incumbent) | kyuz0/amd-strix-halo-comfyui-toolboxes | ROCm | `--pre` from `rocm.nightlies.amd.com/v2-staging/gfx1151` (floating) | **native**, no HSA spoof | essentials, AMDGPUMonitor, GGUF — all unpinned HEAD | 4.4–5.1 GB | **no LICENSE** (all-rights-reserved) | very active, 140★ | digest-only (Dockerfile not reproducible) |
| **IgnatBeresnev/comfyui-gfx1151** | + bluemoehre fork | ROCm | torch 2.9.1 / ROCm 7.2, official `rocm/pytorch` base | native (AMD marks AOTriton gfx1151 *experimental*) | none | — | none | 39★, single maint. | weak (moving base tag) |
| **hec-ovi/comfyui-strix-docker** | hec-ovi/comfyui-strix-docker | ROCm | torch 2.9.1+rocm7.13.0a20260513 triple, TheRock `v2/gfx1151` | native + `HSA_OVERRIDE=11.5.1` **self-pin** (not spoof) + SDMA/SVM fixes | none | build-only | **MIT** glue (GPL-3 image) | 4★, newest (2026-07-03) | **best Dockerfile reproducibility** (exact ABI triple) |
| stable-diffusion.cpp + Vulkan | — | Vulkan (RADV) | N/A (GGML) | — | — | — | — | not ComfyUI | out of scope |

Ruled out (discrete RDNA2/3 or generic ROCm, not gfx1151/APU): weiziqian,
shantur/strix-rocm-all, corundex/ComfyUI-ROCm, HardAndHeavy, gexqin, qinzhen.

## Cross-cutting answers

- **ROCm vs Vulkan for SD/Flux/SDXL/Wan2.2 on gfx1151:** ROCm wins by default —
  no viable Vulkan ComfyUI exists. Do not build a Vulkan variant.
- **gfx1151-native ROCm PyTorch wheels?** Yes, and every real candidate uses
  them (no foreign-chip spoofing). Two sources: AMD's TheRock nightly index
  (`rocm.nightlies.amd.com/v2[-staging]/gfx1151`) and AMD's official
  `rocm/pytorch:rocm7.2...` image (gfx1151 build target since ROCm 7.1, AOTriton
  still *experimental*). `HSA_OVERRIDE_GFX_VERSION=11.5.1` = gfx1151's own ISA
  encoding (self-pin), not spoofing gfx1100/gfx1030.
- **Reproducibility:** kyuz0 = digest-only; IgnatBeresnev = weakest (moving
  base); hec-ovi = strongest Dockerfile-level pins (but no published registry
  image). ⇒ fork the *pattern* (hec-ovi), publish under hal0.

## Recommendation detail

Build FROM a base with explicit ROCm/PyTorch version control (TheRock nightly,
pinned by date+build tag, ABI-matched triple), vendor kyuz0's proven
custom_nodes list pinned to commits, publish `ghcr.io/hal0ai/hal0-comfyui`,
keep `/opt/ComfyUI` + `/opt/venv` so `src/hal0/providers/comfyui.py` needs zero
path changes. Pin `versions.env` `*_REF` to SHAs before first release build
(that floating-HEAD problem is exactly what we're fixing vs kyuz0). CI:
`workflow_dispatch` + weekly schedule against current nightly, smoke test
(`assert 'rocm' in torch.__version__` + `/system_stats`), publish `:<date>` +
`:latest`, app pins the digest.

## Key sources

VERIFIED (read directly): kyuz0/amd-strix-halo-comfyui-toolboxes Dockerfile;
IgnatBeresnev/comfyui-gfx1151 Dockerfile+compose; hec-ovi/comfyui-strix-docker
Dockerfile+README; context7 `/rocm/rocm` compat matrix (gfx1151 AOTriton
experimental); Comfy-Org discussion #5530 (no native Vulkan backend); hal0
`seed_profiles.toml` (kyuz0 digest pin).

REPORTED: ROCm/TheRock#655 (gfx1151 wheel maintenance); ROCm/ROCm#5404, #5890
(AOTriton gap, page-fault bug); tinycomputers.io ROCm 7.0→7.2 gfx1151;
strix-halo-guide (ciru-ai/ROCmFPX identity).
