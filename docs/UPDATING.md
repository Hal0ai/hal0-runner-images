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

Mondays 06:17 UTC, or dispatch. It reads ggml-org/llama.cpp and compares its
newest `b<N>` release with the pin in `runners/standard/manifest.toml`. If it is
newer it opens **one** PR that edits `runners/standard` and `runners/cpu` (they
pin the same release) in both `manifest.toml` (`[source].version`, `[source].ref`)
and the Containerfile `ARG` defaults. An older open bump PR is closed as
superseded.

(ggml-org marks every `b<N>` tag a prerelease, so GitHub's "latest release" is
an unrelated old one. The watch lists releases and takes the highest `b<N>`.)

For `flm`, `comfyui`, `kokoro`, `moonshine` and `qwen3tts` it only reports: the
issue **Runner upstream watch** lists newer upstream versions. To take one, edit
that runner's `manifest.toml` and Containerfile `ARG` lines in an ordinary PR.

## 2. Check the bump: `build.yml` on the PR

Builds only the runners whose `runners/<key>/**` files changed. For each:
`runners/<key>/build.sh --check` if the recipe has one, then a full image build
with `push=false`. A broken bump is red before anyone reads the diff. Require the
**result** job in branch protection; it is the one check that always reports.

## 3. Merge: `build.yml` on main

The same build, then it pushes the immutable tag, attests build provenance
(signed, queryable with `gh attestation verify`) and records a **candidate**:

- a commit comment on the merge commit with runner, tag and digest;
- the job summary;
- a `candidate-<key>` artifact (`<key>.json`, kept 90 days).

A candidate is not pinned anywhere. Nothing that hal0 pulls changes. If the tag
already exists the job fails instead of overwriting it.

To build a runner without changing its files (for example a new package):
dispatch **Build runner images** on `main` with `runners=<key>` and `push=true`.
The same commit can only build a given runner once.

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

Promote refuses a report whose `schema` differs, whose `runner` or `digest` is
not the one being promoted, whose `passed` is not `true`, or that lists any probe
with `passed` other than `true`. Leave host names and addresses out of the
report: this repository is public. A URL that serves something other than JSON
(a PR comment, say) is accepted, and the pin PRs then say "not machine-checked".

## 5. Promote: `promote.yml`

Dispatch on `main` with `runner`, `digest` and `gate_report`. It first verifies:

1. the digest exists in this runner's package;
2. its signed provenance says `build.yml` built it on `refs/heads/main` of this repo;
3. its immutable tag (read from the image's `org.opencontainers.image.version`
   label) still points at that digest;
4. the gate report is valid (above).

Then it opens two PRs and merges neither:

- **this repo:** `images.json` gets that runner's `tag` and `digest`;
- **Hal0ai/hal0:** the runner's row in the pin table of `manifest.json`, and no
  other file (the job aborts if anything else changed).

The field names hal0 expects live in one commented block at the top of
`ci/promote.py` (`HAL0_PIN_TABLE`, `HAL0_FIELDS`, `HAL0_TAG_IS_FULL_REF`).

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

## What CI expects from a recipe

Defined in `ci/lib.py` (docstring). If the recipe layout changes, change it there.

- `runners/<key>/manifest.toml` with `[source]` `repo`, `version`, `ref`.
  `version` is the image tag prefix; `ref` is the git tag or full commit SHA.
- `runners/<key>/Containerfile`, build context `runners/<key>/`. Override with
  `[build] context` / `containerfile`.
- The pinned values appear as `ARG NAME=<value>` defaults in the Containerfile,
  so a bump edits manifest and Containerfile together. A bump fails loudly if it
  finds no such `ARG`.
- `runners/<key>/build.sh --check` (optional) runs before the build.
- `runners/_shared/` is not a runner. A change there rebuilds only runners that
  mention `_shared` in their own files.
- The Containerfile sets `org.opencontainers.image.source` and `.revision` (the
  upstream source hal0 shows as provenance). CI sets `title`, `version`,
  `dev.hal0.runner.key`, `dev.hal0.recipe.revision` and `dev.hal0.recipe.repo`.
