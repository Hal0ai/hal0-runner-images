# Handoff — app-repo cutover (Hal0ai/hal0 lane, not this repo)

This images repo is complete: `hal0-comfyui` is built, smoke-validated on
gfx1151, and published. The remaining work makes the **app actually use it**,
and lives in `Hal0ai/hal0` — owned by the app/cc-web lane, NOT here. Concrete
steps + exact values below. (Anchors verified 2026-07-19; line numbers may
drift — grep the symbol.)

## Published artifact to pin

```
ghcr.io/hal0ai/hal0-comfyui@sha256:fd8c89309ed69e26d0f7ca1483a5775ab0ef336d12f3d62f9a20c7dd0b5d478b
tags: latest, main   (anonymously pullable)
```

## 1. ComfyUI cutover — the O22 deliverable (3 edits, all in Hal0ai/hal0)

**a. Fallback tag** — `src/hal0/runners/__init__.py:74`
```python
# from:
_COMFYUI_IMAGE = "docker.io/kyuz0/amd-strix-halo-comfyui:latest"
# to:
_COMFYUI_IMAGE = "ghcr.io/hal0ai/hal0-comfyui:latest"
```

**b. Manifest pin** — `manifest.json` → `toolbox_images.comfyui` (line ~33)
```json
"comfyui": {
  "tag": "ghcr.io/hal0ai/hal0-comfyui:latest",
  "digest": "sha256:fd8c89309ed69e26d0f7ca1483a5775ab0ef336d12f3d62f9a20c7dd0b5d478b",
  ...
}
```
(Or let CI do it: dispatch build-matrix with `open_manifest_pr=true` — resolves
digests + opens this bump PR automatically. Requires `HAL0_MANIFEST_PR_TOKEN`,
already set.)

**c. Nothing else.** `src/hal0/providers/comfyui.py:56`
`_HAL0_COMFYUI_IMAGE = RUNNER_IMAGES["comfyui"].image` auto-follows edit (a).
The container layout is unchanged — the new image keeps `/opt/ComfyUI` +
`/opt/venv`, matching `_COMFYUI_APP_DIR = "/opt/ComfyUI"` (comfyui.py:63) and
the venv paths. **No `runner_for_backend` / backend split** — the `comfyui`
runner's `backend=None` (runners/__init__.py:168) is correct (research: no
viable Vulkan ComfyUI on gfx1151).

## 2. rocmfpx-hy3 profile repin (host-side, optional hardening)

`[profile.hy3-fpx]` in `/etc/hal0/profiles.toml` (host file, not in the repo)
points at the floating tag `:ifp2-hyv3-mtp`. Repin to reproducible:
```
image = "ghcr.io/hal0ai/hal0-rocmfpx@sha256:dbb443fc888a94f517b01de8200520ac4562459c39e67ccff1626aaa3f0d5f00"
# (or the digest-named tag :ifp2-hyv3-d7e1f26)
```
hy3 slot is `enabled=false` (91 GB weights); low urgency, but the image is now
preserved on ghcr regardless.

## 3. Referenced runners — no action unless rebuilt

`vulkan`/`rocm` (manifest keys) and `rocmfpx` (`DEFAULT_ROCMFPX_IMAGE`,
schema.py:875, `c077206`) already pin published digests. Only re-pin if their
source repos (`amd-strix-halo-toolboxes`, `Hal0_ROCmFPX`) publish new builds.

## 4. Cleanup follow-ups (app repo)

- Remove `packaging/toolbox/*` from Hal0ai/hal0 — build sources now live here.
- Retire `scripts/update-toolbox-digests.sh` — superseded by this repo's
  `scripts/emit-manifest.sh`.
- Not done here to avoid touching `rework/descar` (cc-web owns that CI).

## 5. Verify after cutover

1. `hal0 doctor toolbox-pull` — asserts anonymous pull of the pinned images
   (comfyui is public; confirmed).
2. Bring up an `img` slot (`backend=rocm`) and drive one gen against a real
   checkpoint from `/mnt/ai-models/comfyui/` — the first end-to-end proof the
   app + new image + model mount agree. (Note O25: the renderer mount path bug
   is a separate live fix; don't design around it.)
3. Optional belt-and-suspenders: `podman pull hal0-comfyui:latest` on a box +
   hit `/system_stats` — the published layer was CI-built on x86 from the same
   Dockerfile the gfx1151 smoke used, but wasn't itself run on the APU.
