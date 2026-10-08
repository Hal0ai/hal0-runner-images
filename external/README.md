# Referenced (not vendored) runner sources

Decision 2026-07-19: **reference, don't absorb** — superseded 2026-10-04 for
the amd-strix-halo-toolboxes recipes (now in this repo); still the rule for
Hal0_ROCmFPX. These runner images are
already hal0-owned GitHub forks with their own working CI. This repo pins them
in `../images.json` but does NOT copy their source — that would duplicate two
live repos and compete with their `upstream-sync` workflow. To rebuild/bump,
work in the source repo; then run `../scripts/emit-manifest.sh` to re-pin.

## amd-strix-halo-toolboxes — recipes MOVED here (2026-10-04)

- Repo: https://github.com/Hal0ai/amd-strix-halo-toolboxes (fork of kyuz0/amd-strix-halo-toolboxes)
- The two images hal0 still consumes from it now have their canonical recipe
  in this repo, with llama.cpp pinned to a SHA instead of a branch:
  - `rocm-7.2.4-rocmfp4-server` (the `[base]` of every `../runners/*` recipe) → `../strix-base/`
  - `vulkan-radv-server` (hal0 `FALLBACK_VULKAN_IMAGE`) → `../llama-vulkan/`
- The fork's GHCR package `ghcr.io/hal0ai/amd-strix-halo-toolboxes` must
  **NOT** be deleted: old installs and the runners' pinned base digest
  `sha256:4f5418c1…` pull from it. Its crons are to be disabled and the
  repo archived in a later phase (`../docs/CONSOLIDATION.md`).
- `hal0-toolbox-vulkan:v1` / `hal0-toolbox-rocm:v1` are **not** built by the
  fork (its `build_and_publish.yml` targets kyuz0's Docker Hub; its
  `ghcr-publish.yml` pushes only `amd-strix-halo-toolboxes:*-server`). They
  have no live builder; their hal0 `manifest.json` entries are dead pins.

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
