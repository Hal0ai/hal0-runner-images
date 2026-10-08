# Bumping a runner

How a boutique runner (`runners/*`) or a base recipe (`build-recipes.json`)
moves to a newer source commit, and how one is retired. Owner decision
2026-10-08: build in CI on GitHub-hosted runners; the Strix Halo box only
runs the hardware gate.

## Every runner states why it exists

Each `runners/<dir>/manifest.toml` carries a `[lifecycle]` table, enforced
by `scripts/test_repo_consistency.py`:

| key | meaning |
|---|---|
| `purpose` | what this runner gives that no other runner does |
| `needed_for` | the models or features that require it |
| `retire_when` | the test that ends it, usually "upstream (or another runner) passes the gate for the same models" |
| `track_ref` | the branch on `[source].repo` the fork watch compares the pin against |

A runner with nothing left in `needed_for` is retired, not kept "just in
case". `runners/upstream` is the exception: it is the yardstick the other
runners' `retire_when` is tested against.

## How you hear about new commits

`.github/workflows/fork-watch.yml` runs every Monday. It compares each pin
with its tracked branch through the GitHub compare API and keeps one open
issue, **Fork watch: pinned runner sources**, up to date. It builds nothing.

Alerts mean the pin is at risk, not just old:

- **pin gone**: the pinned commit no longer exists upstream. Rebuilds must
  use the source mirror (below).
- **diverged** or **branch behind**: the branch does not contain the pin.
  Either it was force-pushed or reset, or `track_ref` names a branch the pin
  was never on: check which before acting.
- **branch gone**: the tracked branch was deleted or renamed. Update
  `track_ref` or treat the fork as abandoned.
- **unknown**: the API did not answer. Re-run the workflow; it is not a
  verdict about the fork.

"N new" is information, not a to-do. Bump only when a model or feature you
want needs those commits.

## Bump checklist

One PR per runner. The PR template carries these boxes.

1. **Read the diff.** Open the compare link from the fork-watch issue and
   read what changed between the old pin and the new one. These are third
   party forks compiled into images that run under rootful podman on the
   box: pinning a commit only protects you if someone reads what changed
   before the pin moves. Note anything that touches the network, the
   filesystem outside the model path, or the build scripts.
2. **Update the recipe.** New `[source].ref` (full SHA) and
   `ref_description`. Re-run `bash runners/<dir>/build.sh --check` so the
   patch series is known to apply; drop any patch upstream now carries.
3. **Re-read `[lifecycle]`.** Did the new commits meet `retire_when`, or
   change `needed_for`? Update both in the same PR.
4. **Build in CI.** Run **Build runner images** with `only=runners/<dir>`,
   `push=false` first (build only), then `push=true`. The tag is
   `<dir>-<ref[:7]>-r<commit[:7]>` under `ghcr.io/hal0ai/hal0-runner-<dir>`
   and cannot be overwritten. A push also stores the source as a git bundle
   at `ghcr.io/hal0ai/hal0-source-mirror:<dir>-<ref>`, and for promptforge
   the TheRock tarball at `ghcr.io/hal0ai/hal0-therock-mirror:<version>`.
5. **Hardware gate on the Strix Halo box.** Run the recipe's
   `[verification]` probes against the new digest. A rebuild never inherits
   an earlier gate.
6. **Move the hal0 pin** in a separate hal0 PR that cites the digest, the
   gate result and this PR.

## Rebuilding from the source mirror

If a fork deletes the pinned commit:

```bash
oras pull ghcr.io/hal0ai/hal0-source-mirror:<dir>-<ref> -o /tmp/mirror
git clone /tmp/mirror/source.bundle /tmp/src && git -C /tmp/src checkout <ref>
```

Then point `[source].repo` at a Hal0ai-owned copy of that history before the
next build. The mirror holds what a CI build used; a pin that was never
built in CI has no mirror copy.

`composable_kernel` (promptforge) and the Fedora / ROCm dnf repos are not
mirrored: they come from AMD's and Fedora's official sources, not personal
forks.

## Retiring a runner

When `retire_when` is met: open a hal0 PR that moves the affected slots to
the replacement runner and passes their gate, then delete the
`runners/<dir>` recipe here. Never delete a GHCR package a released hal0
version still pins (`retention-allowlist.json`).
