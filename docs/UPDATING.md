# How a runner gets updated

One path, one approval per bump. Nothing below merges itself: a person merges
the bump PR and both pin PRs, because a pin is a supply-chain change.

Every runner is `runners/<key>/` with a `manifest.toml`, published as
`ghcr.io/hal0ai/hal0-runner-<key>:<upstream-version>-r<recipe-sha7>`. Tags are
immutable: no `latest`, no moving tag, an existing tag is never overwritten.
hal0 pins `image@digest`; the tag is for humans.

```
release-watch  ->  bump PR  ->  merge  ->  candidate  ->  gate  ->  promote  ->  two pin PRs  ->  boxes
(weekly)           build.yml    build.yml  (digest)       (box)     (dispatch)   (merge both)    (hal0 update)
                   push=false   pushes
```

## 1. Find a newer upstream: `release-watch.yml`

Mondays 06:17 UTC, or dispatch. `standard`, `cpu` and the external `cuda` entry
share **one** llama.cpp release pin, `runners/llama.cpp.toml`. The watch compares
ggml-org's newest `b<N>` release with it and, if newer, opens **one** PR that
edits that file and every place that mirrors it (Containerfile `ARG` defaults,
`[source]` tables in runner manifests). An older open bump PR is closed as
superseded.

(ggml-org marks every `b<N>` tag a prerelease, so GitHub's "latest release" is
an unrelated old one. The watch lists releases and takes the highest `b<N>`.)

Every other runner is report-only: `scripts/source_watch.py` writes the report
and the issue **Runner upstream watch** lists newer upstream versions. To take
one, edit that runner's recipe in an ordinary PR.

## 2. Check the bump: `build.yml` on the PR

Builds only the runners whose `runners/<key>/**` files changed (a change to
`runners/llama.cpp.toml` selects the runners that name it). For each:
`python3 scripts/runner.py build <key> --check`, then a full image build with
`push=false`. A broken bump is red before anyone reads the diff. Require the
**result** job in branch protection; it is the one check that always reports.

## 3. Merge: `build.yml` on main

The same build, then it pushes the immutable tag (`scripts/runner.py` computes it:
upstream version plus the last commit that touched `runners/<key>/`), attests
build provenance
(signed, queryable with `gh attestation verify`) and records a **candidate**:

- a commit comment on the merge commit with runner, tag and digest;
- the job summary;
- a `candidate-<key>` artifact (`<key>.json`, kept 90 days).

A candidate is not pinned anywhere. Nothing that hal0 pulls changes. If the tag
already exists, that recipe was already built: the leg is skipped and the tag is
never overwritten.

To build a runner without changing its files (for example a new package):
dispatch **Build runner images** on `main` with `runners=<key>` and `push=true`.

## 4. Gate: the Strix Halo test box

A person runs the hardware gate against the candidate digest:

```
hal0 gate ghcr.io/hal0ai/hal0-runner-<key>@sha256:<digest>
```

The script lives in hal0, not here. Its contract with this repo is a JSON report,
published at an `https` URL, with this shape:

```json
{
  "schema": "hal0.gate-report.v1",
  "runner": "standard",
  "image": "ghcr.io/hal0ai/hal0-runner-standard",
  "digest": "sha256:<64 hex>",
  "tag": "b11552-r1a2b3c",
  "passed": true,
  "date": "2026-10-12T09:00:00Z",
  "lanes": { "rocm": { "passed": true }, "vulkan": { "passed": true } },
  "probes": [ { "name": "health", "passed": true, "detail": "..." } ]
}
```

Promote refuses a report that is not valid `hal0.gate-report.v1` JSON, whose
`runner` or `digest` is not the one being promoted, whose `passed` is not `true`,
or that lists any probe with `passed` other than `true`. There is no "read it by
hand" path. Leave host names and addresses out of the report: this repository is
public.

## 5. Promote: `promote.yml`

Dispatch on `main` with `runner`, `digest` and `gate_report`. It first verifies:

1. the digest exists in this runner's package;
2. its signed provenance says `build.yml` built it on `refs/heads/main` of this repo;
3. its immutable tag (read from the image's `org.opencontainers.image.version`
   label) still points at that digest;
4. the gate report is valid (above).

Then it opens two PRs and merges neither:

- **this repo:** `images.json` gets that runner's `tag` and `digest`, its
  `candidate` is cleared, and `gate_report` and `lanes_gated` are set from the
  report (the lanes whose `passed` is true);
- **Hal0ai/hal0:** the runner's row in the pin table of `manifest.json`, and no
  other file (the job aborts if anything else changed).

A llama.cpp bump moves `standard`, `cpu` and `cuda` together, and hal0's release
gate (`python -m hal0.runners.pins --check-release`) fails while they name
different `b<N>` releases. So promote all three for one bump: three dispatches,
three hal0 PRs (each edits a different row; rebase the later ones). The gate
stays red until the last one merges.

`cuda` has no recipe here (it pins ggml-org's own image). Promote it with the
extra `tag` input; it then checks the digest and tag in the registry but cannot
check build provenance.

The field names hal0 expects live in one commented block, `ci/hal0_pins.py`
(`HAL0_PIN_TABLE`, `HAL0_FIELDS`): hal0's `manifest.json` has a top-level `runners`
object, and each row has exactly `image` (bare repository), `tag` (bare tag),
`digest` and `lanes_gated`. Promote writes those four fields for one runner. The GHCR retention
sweep reads the same table, so it never deletes an image hal0 pins.

## 6. Boxes

hal0's nightly/release carries the new pin. A slot without its own `image_pin`
launches the new digest on its next restart (`hal0 update`).

Rollback is another promote: dispatch it with the previous gated digest (still in
GHCR; the retention sweep does not delete pinned digests or tags it cannot
classify).

## Secrets and settings

| name | where | used by | needs |
|---|---|---|---|
| `HAL0_MANIFEST_PR_TOKEN` | repo secret (exists) | `promote.yml` | fine-grained PAT, Contents + Pull requests: write on `Hal0ai/hal0`. Required |
| `RUNNER_IMAGES_PR_TOKEN` | repo secret (new, optional) | `release-watch.yml`, `promote.yml` | PAT or app token, Contents + Pull requests: write on this repo. Without it the bump and `images.json` PRs open but get no CI run until closed and reopened |
| `HAL0_RETENTION_TOKEN` | repo secret (exists) | `retention.yml` | classic PAT, `read:packages` + `delete:packages` |

Repository settings: Actions workflow permissions read and write, and "Allow
GitHub Actions to create and approve pull requests" on. Each new
`hal0-runner-<key>` package must be set public in GHCR and given this repository
as an Actions write target before the first push, or hal0 cannot pull it
anonymously.

## What CI expects from the recipes side

Defined in the docstring of `ci/lib.py`. If the layout changes, change it there.

- One directory per runner, `runners/<key>/`, with a `manifest.toml`; the
  directory name is the runner key and the package is
  `ghcr.io/hal0ai/hal0-runner-<key>`.
- `scripts/runner.py` is the only build entrypoint: `list --only <keys>` (matrix
  JSON: `key`, `image`, `tag`, `heavy`, `timeout_minutes`), `build <key> --check`
  and `build <key> --tag <ref>` (never pushes).
- The image's `org.opencontainers.image.version` label equals its immutable tag.
  Promote reads the tag from it.
- `runners/llama.cpp.toml` holds the shared pin (`[llama_cpp]` `version`, `ref`);
  the constants are at the top of `ci/release_watch.py`.
- `images.json` is `hal0.runner-images.v2`: a `runners` array keyed by `key`, with
  `image`, `tag`, `digest`, `candidate`, `gate_report`, `lanes_gated`.
