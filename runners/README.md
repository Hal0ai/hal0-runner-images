# runners — hand-built llama-server runner recipes

Canonical home (since 2026-10-04) of the recipes that were
`Hal0ai/hal0 packaging/runner/{rocmfpx,upstream,strix,promptforge}` (copied
at hal0 `d2e8791`). Each directory is `manifest.toml` (every pin: base
digest, source repo + full commit SHA, cmake flags, patch series) +
`build.sh` + `entrypoint.sh` + `patches/`. `upstream/` and `strix/` share
`rocmfpx/build.sh` and `rocmfpx/entrypoint.sh` by symlink.

| dir | image hal0 consumes | hal0 pin |
|---|---|---|
| `rocmfpx/` | `ghcr.io/hal0ai/hal0-combined:0826` | `DEFAULT_ROCMFPX_IMAGE` |
| `upstream/` | `ghcr.io/hal0ai/hal0-combined-upstream:0829` | per-slot `image_pin` only |
| `strix/` | `ghcr.io/hal0ai/hal0-strix-vulkan:0831` @ `sha256:b50a7348…` | `DEFAULT_STRIX_IMAGE` + `manifest.json` |
| `promptforge/` | `ghcr.io/hal0ai/hal0-promptforge:v2.3-qwen38` @ `sha256:370af6e9…` | `DEFAULT_PROMPTFORGE_IMAGE` + `manifest.json` |

Changes from the hal0 copies: a canonical-home note at the top of each
`manifest.toml` and of the two real `build.sh` files; the
`dev.hal0.runner.recipe` image label now names `runners/<dir>`; host names
and operator-local paths replaced with neutral wording. Pins, flags,
patches and entrypoints are unchanged.

## Rules

- `./build.sh --check` (git + python3 + network, no container runtime)
  proves the source ref is reachable and the patch series applies. It is
  not a build. `build-matrix.yml` can run it for all four on dispatch.
- Real builds run in GitHub CI (`build-matrix.yml`, `only=runners/<dir>`),
  which pushes an immutable tag to the recipe's `[ci].image`
  (`ghcr.io/hal0ai/hal0-runner-<dir>`). The Strix Halo box runs only the
  hardware gate. Every recipe sets `GGML_NATIVE=OFF` and names the Zen 5
  ISA (ggml otherwise defaults to `-march=native`), so the CPU code no
  longer depends on the build machine.
  The default tag in each manifest is still the tag hal0 consumes today.
- Every recipe carries `[lifecycle]` (why it exists, when it retires, which
  branch the weekly fork watch tracks). Bumps follow `../docs/BUMPING.md`.
- Always build under a NEW tag (`./build.sh --tag ghcr.io/hal0ai/<pkg>:<new>`).
  Never push over a consumed tag. A rebuild is a new digest and only lands
  through a planned bump that re-runs the hardware gate.
- `[base]` stays on the consumed strix base digest until that bump; the
  recipe for the base itself is `../strix-base/`.
