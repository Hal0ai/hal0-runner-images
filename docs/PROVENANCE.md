# Provenance — where each runner image's build source actually lives

Recovered 2026-07-19 (read-only on lxc105 10.0.1.142 + checks on 141/143 +
ghcr config blobs + Hal0ai GitHub org). Corrects the O22 handoff, which assumed
several sources were missing/un-owned. **They are not** — everything is
hal0-owned; most just wasn't in the app repo.

| Runner | manifest key | Source of truth | Status |
|---|---|---|---|
| cpu | cpu | app repo `packaging/toolbox/cpu.Dockerfile` → `cpu/` here | migrated as-is |
| flm | flm | app repo `packaging/toolbox/flm.Dockerfile` → `flm/` here | migrated as-is |
| qwen3tts | qwen3tts | app repo `packaging/toolbox/qwen3tts*` → `qwen3tts/` here | migrated as-is |
| kokoro | kokoro | **was lost** (only server.py in app repo) — RECONSTRUCTED from `ghcr.io/hal0ai/hal0-toolbox-kokoro:v1` layer history → `kokoro/Dockerfile` | reconstructed, verify before trusting rebuild |
| moonshine | moonshine | **was lost** — RECONSTRUCTED from ghcr config blob (image not on any reachable box) → `moonshine/Dockerfile` | reconstructed, verify |
| comfyui | comfyui | **NEW** hal0-owned `comfyui/Dockerfile` (replaces third-party kyuz0) | drafted, pin `*_REF` before release |
| vulkan | vulkan | `Hal0ai/amd-strix-halo-toolboxes` `toolboxes/Dockerfile.vulkan-radv` (own CI) | referenced, not vendored |
| rocm | rocm | `Hal0ai/amd-strix-halo-toolboxes` `toolboxes/Dockerfile.rocm-7.2.4` (own CI) | referenced |
| rocmfpx/vulkanfpx | — (DEFAULT_ROCMFPX_IMAGE) | `Hal0ai/Hal0_ROCmFPX` `.devops/strix-rocmfp4.Dockerfile` @ `c0772068…` (tag c077206) | referenced; app-side pin |

## Handoff corrections

1. **"vulkan/rocm Dockerfiles missing, find + vendor"** → they're in
   `Hal0ai/amd-strix-halo-toolboxes` (a live hal0 fork of kyuz0 with its own
   build/publish/upstream-sync CI). Reference, don't vendor.
2. **"rocmfpx = external charlie12345 fork, hand-typed pin, scrape 105"** →
   it's `Hal0ai/Hal0_ROCmFPX` (own fork; upstreams charlie12345 + ciru-ai).
   Tag `c077206` = commit `c0772068e0a033da769cf4cca3d1cc4436edc727`, confirmed
   on GH. Builder base `ghcr.io/hal0ai/amd-strix-halo-toolboxes:rocm-7.2.4-rocmfp4-server`.
3. **"kokoro/moonshine move as-is"** → their Dockerfiles were NOT in the app
   repo (only server.py). Reconstructed here from published-image history.
4. **"ciru ComfyUI candidate"** → `ciru-ai/ROCmFPX` is a llama.cpp FPX upstream,
   not ComfyUI.
5. **"comfyui unpinned :latest"** → the app *fallback tag* is `:latest`, but
   `manifest.json.toolbox_images.comfyui` already digest-pins the kyuz0 sha
   `0066678…`. The real gap is ownership/reproducibility, not pinning.

## Recovery method (for audit / re-do)

- lxc105 read-only: `/opt/rocmfpx-src` (FPX checkout, remotes + SHA),
  `/opt/rocmfpx-build/Containerfile.builder`, `podman history` on published
  images.
- ghcr config blob for images absent on-box (moonshine): anonymous token →
  manifest → config blob → `.history[].created_by`.
- **lxc105 (10.0.1.142) was never mutated** — reads only, per operator go.
