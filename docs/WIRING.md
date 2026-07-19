# App wiring — how Hal0ai/hal0 consumes these images

The app resolver is unchanged. `src/hal0/runners/__init__.py:resolve_runner_image`
precedence stays: `HAL0_TOOLBOX_IMAGE_<KEY>` env → `manifest.json` digest pin
(when `runner.manifest_key` set) → bundled `runner.image` fallback. This repo's
CI only patches `manifest.json` digests (via `scripts/emit-manifest.sh`).

## ComfyUI cutover (the O22 deliverable)

**No backend split is needed** — research (docs/comfyui-research-2026-07-19.md)
shows ComfyUI is ROCm-only on gfx1151; there is no viable Vulkan variant. So the
`comfyui` runner keeping `backend=None` is CORRECT. Do NOT mirror
`runner_for_backend` for comfyui; do NOT add `comfyui-rocm`/`comfyui-vulkan`.

The cutover from third-party kyuz0 to hal0-owned is a **one-line-per-site tag
swap**, no resolver change:

1. Publish `ghcr.io/hal0ai/hal0-comfyui:<date>` from `../comfyui/` (pin
   `versions.env` `*_REF` to SHAs first).
2. `src/hal0/runners/__init__.py:74` — change
   `_COMFYUI_IMAGE = "docker.io/kyuz0/amd-strix-halo-comfyui:latest"`
   → `"ghcr.io/hal0ai/hal0-comfyui:latest"` (fallback tag).
3. `manifest.json.toolbox_images.comfyui.tag` → `ghcr.io/hal0ai/hal0-comfyui:latest`;
   `emit-manifest.sh` fills `.digest`. (Today it pins the kyuz0 sha.)
4. No change to `src/hal0/providers/comfyui.py` — it reads the shared resolver;
   `/opt/ComfyUI` + `/opt/venv` layout is preserved by the new image.

`resolve_runner_image` "ignores backend" is a non-issue for comfyui (single
image). It remains relevant only to the llama-server split (vulkan/rocm/fpx),
which is already handled by `runner_for_backend` — out of scope here.

## Owned toolbox digests (flm/kokoro/moonshine/qwen3tts/cpu)

Already `manifest_key`-mapped. This repo's `build-matrix.yml` builds them and
`emit-manifest.sh` re-pins `manifest.json`. Nothing in the app changes.

## Referenced (vulkan/rocm/rocmfpx)

- `vulkan`/`rocm`: built by `Hal0ai/amd-strix-halo-toolboxes` CI; app pins
  `manifest.json.toolbox_images.{vulkan,rocm}` digests (emit-manifest re-pins).
- `rocmfpx`/`vulkanfpx`: app pins `DEFAULT_ROCMFPX_IMAGE`
  (`src/hal0/config/schema.py:875`), NOT manifest.json. Bump that constant by
  hand when `Hal0ai/Hal0_ROCmFPX` publishes a new tag.

## Follow-ups for the app-repo owner (cc-web owns rework/descar CI)

- After migration lands, remove `packaging/toolbox/*` from the app repo and
  point `scripts/update-toolbox-digests.sh` at this repo's emit path (or delete
  it — superseded by `emit-manifest.sh`). Not done here to avoid touching
  rework/descar.
- Verify kokoro/moonshine RECONSTRUCTED Dockerfiles rebuild to the pinned
  digests before deleting the ad-hoc images as the source of truth.
