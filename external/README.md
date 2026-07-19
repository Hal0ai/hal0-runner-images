# Referenced (not vendored) runner sources

Decision 2026-07-19: **reference, don't absorb.** These runner images are
already hal0-owned GitHub forks with their own working CI. This repo pins them
in `../images.json` but does NOT copy their source — that would duplicate two
live repos and compete with their `upstream-sync` workflow. To rebuild/bump,
work in the source repo; then run `../scripts/emit-manifest.sh` to re-pin.

## amd-strix-halo-toolboxes → `vulkan`, `rocm` (+ the rocmfpx builder base)

- Repo: https://github.com/Hal0ai/amd-strix-halo-toolboxes (fork of kyuz0/amd-strix-halo-toolboxes)
- Builds + pushes via its own `.github/workflows/build_and_publish.yml` + `ghcr-publish.yml`; `upstream-sync.yml` tracks kyuz0.
- Dockerfiles: `toolboxes/Dockerfile.vulkan-radv`, `toolboxes/Dockerfile.rocm-7.2.4`,
  `toolboxes/Dockerfile.rocm-7.2.4-rocmfp4-server` (the rocmfpx builder base), plus
  `toolboxes/hip-rocm7rc.patch`, `toolboxes/llama-grammar.patch`.
- App pins: `manifest.json.toolbox_images.{vulkan,rocm}` (tag `v1`).

## Hal0_ROCmFPX → `rocmfpx` / `vulkanfpx`

- Repo: https://github.com/Hal0ai/Hal0_ROCmFPX (llama.cpp FPX fork; upstreams: charlie12345/ROCmFPX, ciru-ai/ROCmFPX)
- The primary LLM runner actually serving on the boxes.
- Build: `.devops/strix-rocmfp4.Dockerfile` (FROM `rocm/dev-ubuntu-24.04:7.2.1-complete`, gfx1151)
  via `scripts/build-strix-rocmfp4-mtp.sh`; builder toolchain `.devops`-adjacent
  `Containerfile.builder` FROM `ghcr.io/hal0ai/amd-strix-halo-toolboxes:rocm-7.2.4-rocmfp4-server`.
- Pinned tag `c077206` = commit `c0772068e0a033da769cf4cca3d1cc4436edc727` (confirmed on GH main lineage).
- App pin: `DEFAULT_ROCMFPX_IMAGE` in `src/hal0/config/schema.py` (NOT manifest.json.toolbox_images).

### Note: "ciru" is not a ComfyUI image
`ciru-ai/ROCmFPX` is one of the llama.cpp FPX upstreams (a git remote on the
FPX checkout), not a ComfyUI fork. The O22 handoff's "ciru ComfyUI candidate"
was a cross-wire. The only ComfyUI work is `../comfyui/`.
