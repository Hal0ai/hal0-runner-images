# images.json

The catalogue of runner images. hal0 reads it from
`https://raw.githubusercontent.com/Hal0ai/hal0-runner-images/main/images.json`.
It lists only the runners hal0 can launch today, one entry each.
`scripts/test_repo_consistency.py` checks every rule below.

## Format: `hal0.runner-images.v2`

```json
{
  "schema": "hal0.runner-images.v2",
  "runners": [
    {
      "id": "standard",
      "title": "Standard",
      "summary": "llama.cpp for AMD GPUs, ROCm or Vulkan, built from the official upstream release.",
      "image": "ghcr.io/hal0ai/hal0-runner-standard",
      "tag": "b11552-r1a2b3c4",
      "digest": "sha256:…",
      "backends": ["rocm", "vulkan"],
      "family": "llama-server",
      "tier": "default",
      "recipe": "runners/standard",
      "maintainer_notes": "Free text for maintainers."
    }
  ]
}
```

The top level has exactly two keys, `schema` and `runners`. Each entry has
exactly these fields, in this order:

| field | type | meaning |
|---|---|---|
| `id` | string | The runner key: hal0's runner key, the recipe directory name, and the `<key>` in `hal0-runner-<key>`. Lowercase letters and digits. |
| `title` | string | The name people see, e.g. "Standard". |
| `summary` | string | One plain line: what it is and when to pick it. |
| `image` | string | The image repository, without a tag. `ghcr.io/hal0ai/hal0-runner-<id>` for every runner built here. |
| `tag` | string or null | The tag of the build hal0 should pin, `<upstream version>-r<recipe sha7>`. `null` until a build has passed the gate. |
| `digest` | string or null | `sha256:<64 hex>` of that tag. `null` until gated. A digest always comes with its tag. |
| `backends` | list of strings | What the runner can execute: any of `rocm`, `vulkan`, `cpu`, `npu`, `cuda`. |
| `family` | string | The runtime the image serves, hal0's `RuntimeFamily`: `llama-server`, `flm`, `kokoro`, `moonshine`, `qwen3tts` or `comfyui`. |
| `tier` | string | `default` (installed and offered by default) or `optional` (opt-in). |
| `recipe` | string or null | `runners/<id>` for an image built here. `null` marks an **external** image hal0 pins but this repo does not build (today only `cuda`, ggml-org's official `server-cuda` image). |
| `maintainer_notes` | string | Notes for people maintaining the image. hal0 does not show them. |

## Rules

- `title`, `summary`, `image`, `backends`, `family` and `tier` must equal the
  `[runner]` and `[image]` tables of `runners/<id>/manifest.toml`. The
  manifest is where they are edited; the catalogue copies them.
- For a built runner, `tag` starts with the manifest's `[image].version`.
- `tag` and `digest` change only when a gated build is pinned
  (docs/UPDATING.md step 5). Building an image never edits this file.
- An external entry cannot use a `hal0-runner-` image name.
- No other fields. Prose goes in `maintainer_notes` or the recipe's manifest.

## Changes from v1

`hal0.runner-images.v1` had an `images` array with `ownership`, `publish`,
`manifest_key`, `build`, `notes`, `runtime_family`, `supported_backends`,
`display_name`, `app_pin`, `published`, `moving_tag` and a top-level
`_comment`, and listed retired images. v2 renames the array to `runners`,
`runtime_family` to `family` and `supported_backends` to `backends`; adds
`title`, `summary`, `tier`, `recipe` and `maintainer_notes`; makes `tag` and
`digest` the gated pin; and drops every other field. hal0 releases that only
understand v1 cannot parse v2 and show GHCR-discovered rows only.
