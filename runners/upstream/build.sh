#!/usr/bin/env bash
# Build runners/upstream from manifest.toml + Containerfile.
#
#   ./build.sh                 # build the tag named in the manifest
#   ./build.sh --tag foo:bar   # build under a different tag (CI, candidates)
#   ./build.sh --check         # clone + apply the patch series, build nothing
#
# Same contract as ../rocmfpx/build.sh (which the other runners share by
# symlink): the manifest is the single source of truth, the source is
# checked out at the pinned SHA on the host and asserted, patches apply on
# the host, --check needs only git + python3. The difference is that the
# image definition is the tracked Containerfile next to this file; this
# script only turns manifest values into --build-arg.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="${HERE}/manifest.toml"
WORK="${HAL0_RUNNER_BUILD_DIR:-/tmp/hal0-upstream-build}"
TAG_OVERRIDE=""
CHECK_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag) TAG_OVERRIDE="$2"; shift 2 ;;
        --check) CHECK_ONLY=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
done

read_manifest() {
    python3 - "$MANIFEST" "$1" <<'PY'
import sys, tomllib
doc = tomllib.load(open(sys.argv[1], "rb"))
cur = doc
for part in sys.argv[2].split("."):
    cur = cur[part]
if isinstance(cur, list):
    print("\n".join(str(x) for x in cur))
else:
    print(cur)
PY
}

TAG="${TAG_OVERRIDE:-$(read_manifest image.tag)}"
REPO="$(read_manifest source.repo)"
REF="$(read_manifest source.ref)"
mapfile -t CMAKE_FLAGS < <(read_manifest build.cmake_flags)
mapfile -t PATCHES < <(python3 - "$MANIFEST" <<'PY'
import sys, tomllib
doc = tomllib.load(open(sys.argv[1], "rb"))
for p in doc.get("patches", []):
    print(p["file"])
PY
)

echo "==> tag     ${TAG}"
echo "==> source  ${REPO} @ ${REF}"
echo "==> rocm    $(read_manifest base.rocm_series) ($(read_manifest base.rocm_nevr))"
echo "==> patches ${#PATCHES[@]}"

# Stage dir IS the build context: src/ + the entrypoint, nothing else.
STAGE="${WORK}/stage"
SRC="${STAGE}/src"
rm -rf "$STAGE"; mkdir -p "$STAGE"
# GitHub serves any reachable commit by SHA, so a depth-1 fetch of the pin
# is enough (and ~10x smaller than the full clone the fork recipes need).
# Fall back to a full clone for a host that does not.
git init -q "$SRC"
git -C "$SRC" remote add origin "$REPO"
if ! git -C "$SRC" fetch -q --depth 1 origin "$REF"; then
    rm -rf "$SRC"
    git clone -q --no-checkout "$REPO" "$SRC"
fi
git -C "$SRC" checkout -q "$REF"
HEAD_SHA="$(git -C "$SRC" rev-parse HEAD)"
[[ "$HEAD_SHA" == "$REF" ]] || {
    echo "checkout landed on ${HEAD_SHA}, manifest pins ${REF}" >&2; exit 66
}
echo "==> checked out ${HEAD_SHA}"

for p in "${PATCHES[@]}"; do
    git -C "$SRC" apply --check "${HERE}/patches/${p}"
    git -C "$SRC" apply "${HERE}/patches/${p}"
    echo "==> applied ${p}"
done

if (( CHECK_ONLY )); then
    echo "==> --check: source reachable and the patch series applies against ${REF}; nothing built"
    exit 0
fi

RUNTIME="${HAL0_CONTAINER_RUNTIME:-$(command -v docker || command -v podman || true)}"
[[ -n "$RUNTIME" ]] || { echo "no docker/podman on PATH" >&2; exit 65; }

cp "${HERE}/entrypoint.sh" "${STAGE}/hal0-runner-entrypoint.sh"

RECIPE_DIR="runners/$(basename "$HERE")"
RECIPE_REV="$(git -C "$HERE" rev-parse HEAD 2>/dev/null || echo unknown)"
if ! git -C "$HERE" diff --quiet HEAD -- "$HERE" 2>/dev/null; then
    RECIPE_REV="${RECIPE_REV}-dirty"
fi
PATCH_SERIES_SHA="$(
    for p in "${PATCHES[@]}"; do cat "${HERE}/patches/${p}"; done | sha256sum | cut -d' ' -f1
)"

"$RUNTIME" build -f "${HERE}/Containerfile" -t "$TAG" \
    --build-arg "FEDORA_BUILDER=$(read_manifest base.builder_image)@$(read_manifest base.builder_digest)" \
    --build-arg "FEDORA_RUNTIME=$(read_manifest base.image)@$(read_manifest base.digest)" \
    --build-arg "ROCM_REPO=$(read_manifest base.rocm_repo)" \
    --build-arg "ROCM_GPG=$(read_manifest base.rocm_gpg)" \
    --build-arg "ROCM_SERIES=$(read_manifest base.rocm_series)" \
    --build-arg "ROCM_NEVR=$(read_manifest base.rocm_nevr)" \
    --build-arg "CMAKE_FLAGS=${CMAKE_FLAGS[*]}" \
    --build-arg "JOBS=${JOBS:-4}" \
    --build-arg "SOURCE_REPO=${REPO}" \
    --build-arg "SOURCE_REF=${REF}" \
    --build-arg "RECIPE_DIR=${RECIPE_DIR}" \
    --build-arg "RECIPE_REV=${RECIPE_REV}" \
    --build-arg "PATCHES=$(IFS=,; echo "${PATCHES[*]}")" \
    --build-arg "PATCHES_SHA256=${PATCH_SERIES_SHA}" \
    "$STAGE"
echo "==> built ${TAG}"
