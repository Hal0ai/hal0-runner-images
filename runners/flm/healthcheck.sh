#!/bin/sh
# hal0-flm-healthcheck: probes the port flm is actually serving on.
#
# hal0's FLM provider never exports HAL0_PORT into the container; it only
# passes the port as `flm serve <tag> --port N` on argv. Resolution order:
#   1. $HAL0_PORT, if set and non-empty.
#   2. The --port value from the running `flm ... serve ...` process's
#      cmdline, found by scanning /proc/[0-9]*/cmdline.
#   3. 8086 (the image's documented default / EXPOSE port).
#
# HAL0_FLM_PROC overrides the /proc root, for testing only; defaults to /proc.
# POSIX sh only (no bash-isms) — the runtime image has no bash guarantee and
# no procps (no ps/pgrep), so we read /proc/<pid>/cmdline directly. Reading
# another process's cmdline under the same uid is always permitted.

set -eu

proc_root="${HAL0_FLM_PROC:-/proc}"

port=""

if [ -n "${HAL0_PORT:-}" ]; then
    port="$HAL0_PORT"
fi

if [ -z "$port" ]; then
    for cmdline in "$proc_root"/[0-9]*/cmdline; do
        [ -f "$cmdline" ] || continue

        # NUL-separated argv -> one element per line.
        argv="$(tr '\0' '\n' < "$cmdline" 2>/dev/null)" || continue
        [ -n "$argv" ] || continue

        arg0="$(printf '%s\n' "$argv" | sed -n '1p')"
        arg1="$(printf '%s\n' "$argv" | sed -n '2p')"

        is_flm=0
        case "$arg0" in
            */flm|flm) is_flm=1 ;;
        esac
        case "$arg1" in
            */flm|flm) is_flm=1 ;;
        esac
        [ "$is_flm" -eq 1 ] || continue

        if ! printf '%s\n' "$argv" | grep -qx 'serve'; then
            continue
        fi

        candidate="$(printf '%s\n' "$argv" | awk '
            { lines[NR] = $0 }
            END {
                for (i = 1; i <= NR; i++) {
                    if (lines[i] == "--port" && i < NR) {
                        print lines[i + 1]
                        exit
                    }
                    if (index(lines[i], "--port=") == 1) {
                        print substr(lines[i], 8)
                        exit
                    }
                }
            }')"

        case "$candidate" in
            ''|*[!0-9]*) continue ;;
        esac

        port="$candidate"
        break
    done
fi

if [ -z "$port" ]; then
    port=8086
fi

exec curl -fsS --max-time 4 "http://127.0.0.1:${port}/v1/models" >/dev/null
