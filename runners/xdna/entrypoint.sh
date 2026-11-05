#!/usr/bin/env bash
# hal0-xdna-entrypoint: preflight + exec for hal0-runner-xdna.
#
# Contract with hal0 (slots/argv.py for the npu/xdna lane):
#   - binary:    /opt/xdna/bin/llama-server, symlinked to /usr/local/bin/llama-server
#   - devices:   /dev/accel (XDNA 2 NPU), /dev/dri (iGPU helpers)
#   - ulimits:   --ulimit memlock=-1:-1 (NPU buffer pinning)
#   - argv:      --device XDNA0 -ngl 99 -fa on --poll 0, plus -m <model> etc.
#
# This script never fails the preflight on something the box controls
# (NPU device node, fabric clock pstate) — a missing /dev/accel is normal
# for a build-time or CPU-only smoke run of this same image.
set -euo pipefail

echo "[xdna-entrypoint] XRT libs:"
ldconfig -p | grep -E 'libxrt|libxdna' || echo "[xdna-entrypoint]   (none resolved via ldconfig)"

if [ -e /dev/accel/accel0 ]; then
    echo "[xdna-entrypoint] /dev/accel/accel0 present"
else
    echo "[xdna-entrypoint] WARNING: /dev/accel/accel0 not found (expected for a CPU-only or build-time run)"
fi

# Fabric clock pstate: ggml-xdna's decode throughput depends heavily on it
# (spike: turbo vs default pmode is +25% tg128). This only warns — the
# runner has no permission (and no business) changing host pstate itself.
for fclk in /host/sys/class/drm/card*/device/pp_dpm_fclk; do
    [ -r "$fclk" ] || continue
    if ! grep -q '\*$' "$fclk" 2>/dev/null; then
        top="$(tail -n1 "$fclk" 2>/dev/null || true)"
        echo "[xdna-entrypoint] WARNING: fabric clock ($fclk) is not pinned to its top state ($top); NPU throughput will be lower"
    fi
    break
done

# Some host XRT builds (e.g. a newer PPA snapshot the box already runs) may
# need to take priority over the image's own XRT for device compatibility.
# Unset/empty by default — the image's XRT is what was bisected for
# correctness (see docs/superpowers/plans/xdna-runner.md).
if [ -n "${HAL0_XDNA_HOST_XRT:-}" ]; then
    echo "[xdna-entrypoint] HAL0_XDNA_HOST_XRT set: prepending $HAL0_XDNA_HOST_XRT to LD_LIBRARY_PATH"
    export LD_LIBRARY_PATH="${HAL0_XDNA_HOST_XRT}:${LD_LIBRARY_PATH:-}"
fi

exec /opt/xdna/bin/llama-server "$@"
