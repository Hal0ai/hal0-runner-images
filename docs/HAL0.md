# How hal0 uses these images

## What hal0 reads

- **The catalogue.** hal0's Runner Images page syncs `images.json` from this
  repo's `main` branch (docs/SCHEMA.md) and asks GHCR anonymously for each
  image's tags, digests and sizes. It shows one row per entry.
- **The pin.** hal0 itself decides which image a runner launches: each
  runner key maps to an `image:tag@digest` in hal0's own code. That pin moves
  only through a hal0 pull request, after the build passed its gate here
  (docs/UPDATING.md). The catalogue's `tag` and `digest` record the same
  gated build so both repos agree, and `lanes_gated` lists the backends that
  passed the gate for it: hal0 offers a runner on a lane (for example Vulkan)
  only when that lane is listed, instead of keeping its own list.
- **Image labels.** Every image carries:

  | label | value |
  |---|---|
  | `org.opencontainers.image.title` | the runner key |
  | `org.opencontainers.image.version` | the tag, `<version>-r<sha7>` |
  | `org.opencontainers.image.source` | the upstream source repository |
  | `org.opencontainers.image.revision` | the upstream commit (or `package==version` for PyPI sources) |
  | `dev.hal0.runner.key` | the runner key |
  | `dev.hal0.recipe.repo` / `dev.hal0.recipe.revision` | this repo and the recipe commit the image was built from |
  | `dev.hal0.runner.patches` | comma-separated patch file names (empty when none) |

  hal0 shows `source`, `revision` and the patch count as provenance.

## What each image expects from hal0

hal0 starts every runner as a podman container and passes the slot's
arguments only, never the program name.

| runner | entrypoint | notes |
|---|---|---|
| `standard` | `/opt/hal0/entrypoint.sh`, which runs `/opt/hal0/bin/llama-server` | Passes `--port 8080` if the arguments have no `--port` (llama-server's own default port is 9931, upstream #30159). Exits 78 with a readable message when GPU offload is requested but no `/dev/kfd` or `/dev/dri/renderD*` is mapped, and 64 when llama-server dies before `/health` ever answered, so systemd stops restarting a model that cannot load. `llama-bench`, `llama-cli`, `llama-quantize` and `llama-perplexity` are in `/opt/hal0/bin`. |
| `cpu` | `/opt/hal0/bin/llama-server` | Runs as uid/gid 1000 (`hal0`). hal0 must pass `--port`. |
| `flm` | `tini -- /usr/local/bin/flm` | Needs `/dev/accel`, `/dev/dri` and `--ulimit memlock=-1:-1`. Model cache in `~/.config/flm` (uid 1000). |
| `kokoro` | `tini -- kokoro-server` | Port 8090 by default; uid 1000. |
| `moonshine` | `tini -- moonshine-server` | Port 8089 by default; uid 1000. |
| `qwen3tts` | `python3 /opt/qwen3tts/qwen3tts_server.py` | Needs `/dev/kfd` and `/dev/dri`; Hugging Face cache in `/cache`. |
| `comfyui` | none (`CMD /bin/bash`) | hal0 supplies the ComfyUI command line and `extra_model_paths.yaml`. ComfyUI is in `/opt/ComfyUI`, the venv in `/opt/venv`. |
| `cuda` | ggml-org's image | Not built here. |

## Boxes installed before the fresh runner set

Earlier hal0 releases pull images from the old packages (`hal0-combined`,
`hal0-toolbox-*`, `hal0-comfyui`, `hal0-strix-vulkan`, `hal0-promptforge`,
`hal0-rocmfpx`). They stay on GHCR, unlisted, and
`retention-allowlist.json` stops the cleanup from deleting the versions those
releases pin. hal0 moves boxes to the new images in one migration; after that
release is the oldest supported one, the operator deletes the old packages
and the allowlist entries go with them.
