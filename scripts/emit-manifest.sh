#!/usr/bin/env bash
#
# emit-manifest.sh — resolve the published ghcr digest of every runner image
# declared in images.json and patch them into the hal0 APP repo's manifest.json.
#
# This is the migrated + generalised successor to hal0's in-app
# scripts/update-toolbox-digests.sh. The difference: the source of truth for
# WHICH images exist is now images.json IN THIS REPO, and the target it
# patches is the app repo's manifest.json (checked out elsewhere, path passed
# as $1). In CI this runs after the build-matrix pushes images, then opens a
# manifest-bump PR against Hal0ai/hal0.
#
# For each image with a non-null manifest_key it resolves the ghcr content
# digest anonymously and sets manifest.json toolbox_images.<manifest_key>.digest.
# Images with manifest_key == null (rocmfpx/vulkanfpx: app-side DEFAULT_*_IMAGE
# pins, not manifest.json) are reported but not patched — bump those by hand.
#
# Usage:
#   scripts/emit-manifest.sh <path-to-app-manifest.json>
#
# Requirements: bash, python3, curl. Optional fallback: docker buildx.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGES_JSON="${REPO_ROOT}/images.json"
APP_MANIFEST="${1:-}"

if [[ -z "${APP_MANIFEST}" ]]; then
    echo "usage: $0 <path-to-app-manifest.json>" >&2
    exit 2
fi
if [[ ! -f "${IMAGES_JSON}" ]]; then
    echo "error: images.json not found: ${IMAGES_JSON}" >&2
    exit 1
fi
if [[ ! -f "${APP_MANIFEST}" ]]; then
    echo "error: app manifest not found: ${APP_MANIFEST}" >&2
    exit 1
fi
command -v python3 >/dev/null 2>&1 || { echo "error: python3 required" >&2; exit 1; }
command -v curl    >/dev/null 2>&1 || { echo "error: curl required"    >&2; exit 1; }

ACCEPT_HEADER='application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'

# Emit "<manifest_key>\t<image>:<tag>\t<ownership>" for images that map to a
# manifest_key; images with null manifest_key are printed to stderr as skips.
list_images() {
    python3 - "${IMAGES_JSON}" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
for key, e in (doc.get("images") or {}).items():
    mk = e.get("manifest_key")
    ref = f'{e.get("image","")}:{e.get("tag","")}'
    if mk:
        print(f'{mk}\t{ref}\t{e.get("ownership","")}')
    else:
        sys.stderr.write(f'skip (no manifest_key, app-side pin): {key} -> {ref} '
                         f'[{e.get("app_pin","?")}]\n')
PY
}

# Resolve a ghcr.io content digest for "ghcr.io/owner/name:ref" anonymously.
resolve_digest() {
    local image_ref="$1" registry repo_ref repo reference token digest
    registry="${image_ref%%/*}"
    repo_ref="${image_ref#*/}"
    if [[ "${repo_ref}" == *:* ]]; then
        repo="${repo_ref%:*}"; reference="${repo_ref##*:}"
    else
        repo="${repo_ref}"; reference="latest"
    fi
    if [[ "${registry}" != "ghcr.io" ]]; then
        echo "warn: ${image_ref}: only ghcr.io supported by the curl path" >&2
    fi
    token="$(curl -fsSL \
        "https://ghcr.io/token?scope=repository:${repo}:pull&service=ghcr.io" \
        2>/dev/null | python3 -c \
        'import json,sys; print(json.load(sys.stdin).get("token",""))' \
        2>/dev/null || true)"
    if [[ -n "${token}" ]]; then
        digest="$(curl -fsSI -X GET \
            -H "Authorization: Bearer ${token}" \
            -H "Accept: ${ACCEPT_HEADER}" \
            "https://${registry}/v2/${repo}/manifests/${reference}" 2>/dev/null \
            | tr -d '\r' \
            | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}' \
            | tail -n1 || true)"
        [[ "${digest}" == sha256:* ]] && { printf '%s\n' "${digest}"; return 0; }
    fi
    if command -v docker >/dev/null 2>&1; then
        digest="$(docker buildx imagetools inspect "${image_ref}" 2>/dev/null \
            | awk -F': ' 'tolower($1)=="digest"{print $2}' | head -n1 || true)"
        [[ "${digest}" == sha256:* ]] && { printf '%s\n' "${digest}"; return 0; }
    fi
    return 1
}

# Patch toolbox_images.<key>.{tag,digest} in the app manifest.
patch_digest() {
    local key="$1" tag="$2" digest="$3"
    python3 - "${APP_MANIFEST}" "${key}" "${tag}" "${digest}" <<'PY'
import json, sys
path, key, tag, digest = sys.argv[1:5]
m = json.load(open(path))
entry = m.setdefault("toolbox_images", {}).setdefault(key, {})
if tag:
    entry["tag"] = tag
entry["digest"] = digest or None
json.dump(m, open(path, "w"), indent=2, ensure_ascii=False)
open(path, "a").write("\n")
PY
}

echo "Emitting digests from ${IMAGES_JSON} into ${APP_MANIFEST}"
updated=0; warned=0
while IFS=$'\t' read -r key ref ownership; do
    [[ -z "${key}" ]] && continue
    tag="${ref##*:}"
    echo "  ${key} (${ownership}): resolving ${ref}"
    if digest="$(resolve_digest "${ref}")" && [[ -n "${digest}" ]]; then
        patch_digest "${key}" "${tag}" "${digest}"
        echo "    -> ${digest}"
        updated=$((updated + 1))
    else
        echo "warn: ${key}: ${ref} unpublished/unreachable — leaving digest null" >&2
        patch_digest "${key}" "${tag}" ""
        warned=$((warned + 1))
    fi
done < <(list_images)

echo "Done: ${updated} updated, ${warned} left null."
echo "Review 'git diff ${APP_MANIFEST}' in the app checkout and open a bump PR."
