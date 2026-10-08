# Provenance — where each runner image's build source actually lives

> 2026-10-04: recipes consolidated into this repo; see `CONSOLIDATION.md`.
> The 2026-07-19 text below is kept as the historical record.

Recovered 2026-07-19 (read-only on the lab gate box + checks on the other lab
boxes + ghcr config blobs + Hal0ai GitHub org). Corrects the O22 handoff, which assumed
several sources were missing/un-owned. **They are not** — everything is
hal0-owned; most just wasn't in the app repo.

| Runner | manifest key | Source of truth | Status |
|---|---|---|---|
| cpu | cpu | app repo `packaging/toolbox/cpu.Dockerfile` → `cpu/` here | re-synced 2026-10-04 to hal0's CPU-only GGML_NATIVE=OFF build (hal0#2126) |
| flm | flm | app repo `packaging/toolbox/flm.Dockerfile` → `flm/` here | migrated as-is |
| qwen3tts | qwen3tts | app repo `packaging/toolbox/qwen3tts*` → `qwen3tts/` here | migrated as-is |
| kokoro | kokoro | **was lost** (only server.py in app repo) — RECONSTRUCTED from `ghcr.io/hal0ai/hal0-toolbox-kokoro:v1` layer history → `kokoro/Dockerfile` | reconstructed + **rebuild-verified** 2026-07-19 (serves, /health ok) |
| moonshine | moonshine | **was lost** — RECONSTRUCTED from ghcr config blob (image not on any reachable box) → `moonshine/Dockerfile` | reconstructed + **rebuild-verified** 2026-07-19 (serves, /health ok) |
| comfyui | comfyui | **NEW** hal0-owned `comfyui/Dockerfile` (replaces third-party kyuz0) | published 2026-07-19; all `*_REF` pinned to commit SHAs |
| vulkan | vulkan | historical `toolboxes/Dockerfile.vulkan-radv`; **no live builder** publishes `hal0-toolbox-vulkan` | dead pin (see images.json) |
| rocm | rocm | historical `toolboxes/Dockerfile.rocm-7.2.4`; **no live builder** publishes `hal0-toolbox-rocm` | dead pin (see images.json) |
| strix base | — (runners' `[base]` digest) | `strix-base/` here (moved from the fork 2026-10-04, source pinned) | canonical here |
| fork vulkan-radv-server | — (FALLBACK_VULKAN_IMAGE) | `llama-vulkan/` here (moved from the fork 2026-10-04, source pinned) | canonical here |
| rocmfpx-combined / upstream / strix / promptforge | DEFAULT_*_IMAGE + manifest | `runners/*` here (moved from hal0 `packaging/runner/` 2026-10-04) | canonical here |
| rocmfpx/vulkanfpx (c077206) | — (former DEFAULT_ROCMFPX_IMAGE) | `Hal0ai/Hal0_ROCmFPX` `.devops/strix-rocmfp4.Dockerfile` @ `c0772068…` (tag c077206) | superseded by `runners/rocmfpx` (`hal0-combined:0826`); kept as #1948 evidence |
| rocmfpx-hy3 | — (profile.hy3-fpx) | `Hal0ai/Hal0_ROCmFPX` `.devops/strix-rocmfp4.Dockerfile` @ `d7e1f26`, builder `localhost/hal0-rocmfpx-builder:7.2.4` | referenced; digest-pinned |

## rocmfpx-hy3 — rescued from local-only (2026-07-19)

The Hy3 FPX runner (`ghcr.io/hal0ai/hal0-rocmfpx:ifp2-hyv3-{mtp,d7e1f26}`) was
built 2026-07-14 on the lab gate box and existed **only in its local podman store** — not
on ghcr. Verified missing from the remote (`tags/list` had c077206, 5b39566,
7aa484a, server, vulkan-minicpm5 — no hyv3), then pushed off-box 2026-07-19.

- Manifest digest (both tags): `sha256:dbb443fc888a94f517b01de8200520ac4562459c39e67ccff1626aaa3f0d5f00`
- Config / image id: `57aaafb773b1`, 7.53 GB.
- Recipe: IFP2 (ggml type107) + minicpm5 + hy_v3 NextN/MTP patch, so
  Hy3-Chadrock-FPX-IFP2-MTP loads. Source `Hal0_ROCmFPX @ d7e1f26`; builder
  toolchain `localhost/hal0-rocmfpx-builder:7.2.4` (rebuild recipe — push it to
  ghcr too if you want that preserved).
- App follow-up: `[profile.hy3-fpx]` in `/etc/hal0/profiles.toml` references the
  floating `:ifp2-hyv3-mtp` tag — repin to `:ifp2-hyv3-d7e1f26` (or the digest)
  for reproducibility. hy3 slot is `enabled=false` (91 GB weights can't
  co-reside with the agent slot on 128 GB).

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

- lab gate box, read-only: the FPX source checkout (remotes + SHA), the
  builder Containerfile, `podman history` on published images.
- ghcr config blob for images absent on-box (moonshine): anonymous token →
  manifest → config blob → `.history[].created_by`.
- **the lab gate box was never mutated** — reads only, per operator go.
