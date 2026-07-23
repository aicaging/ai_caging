#!/usr/bin/env bash
# init.sh — Caging deployment thin wrapper
#
# Delegates all deployment logic to _init.py.
# _init.py always reads plan.yaml (for AI defaults); single-link
# mode uses --link flag instead of temp plan files.
#
# Usage:
#   Plan-based:   sudo ./init.sh [--user <name>...]
#   Nginx portal: sudo ./init.sh -nginx [--portal-port N] [--dry-run]
#   Single-link:  sudo ./init.sh [-port N] [-username U] [-password P] \
#                                 -service <parent> <child>
#                 sudo ./init.sh [-port N] -cage <parent> <child>
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Must run as root (sudo)"
    exit 1
fi

# ────────────────────────────────────────────────────────────
# Detect mode: plan-based (no positional flags) or single-link
# ────────────────────────────────────────────────────────────
# Normalise shorthand flags before passing to _init.py
case "${1:-}" in
    -nginx)
        shift
        echo "Stopping all caging services..."
        systemctl list-units --type=service --all --no-legend 2>/dev/null \
            | awk '/caging@.*\.service/ {print $1}' \
            | while IFS= read -r svc; do
                systemctl stop "$svc" 2>/dev/null || true
                echo "  stopped $svc"
            done
        echo ""

        python3 "${PROJECT_DIR}/_init.py" --nginx "$@"

        echo ""
        echo "Starting all caging services..."
        systemctl list-units --type=service --all --no-legend 2>/dev/null \
            | awk '/caging@.*\.service/ {print $1}' \
            | while IFS= read -r svc; do
                systemctl start "$svc" 2>/dev/null || true
                echo "  started $svc"
            done
        exit $?
        ;;
    -dry-run)   shift; exec python3 "${PROJECT_DIR}/_init.py" --dry-run "$@" ;;
esac

if [ $# -eq 0 ] || [[ "$1" != "-service" && "$1" != "-cage" ]]; then
    # Plan-based mode — pass all args to _init.py as-is
    exec python3 "${PROJECT_DIR}/_init.py" "$@"
fi

# ────────────────────────────────────────────────────────────
# Single-link mode — translate to _init.py --link args
# Original: -service|-cage <parent> <child> [parent_url] [flags]
# Becomes:  --link service|cage <parent> <child> [--port N] ...
# ────────────────────────────────────────────────────────────
MODE="${1#-}"   # strip leading dash: -service → service
shift
PARENT="$1"
CHILD="$2"
shift 2
# Skip parent_url (4th positional arg, now derived from plan.yaml)
[ $# -ge 1 ] && [[ "$1" != -* ]] && shift

PY_ARGS=(--link "$MODE" "$PARENT" "$CHILD")

while [[ $# -gt 0 ]]; do
    case "$1" in
        -port)          PY_ARGS+=(--port "$2");      shift 2 ;;
        -username|--user) PY_ARGS+=(--username "$2"); shift 2 ;;
        -password|--pass) PY_ARGS+=(--password "$2"); shift 2 ;;
        *)              shift ;;  # skip unknown flags
    esac
done

echo "============================================"
echo " Caging ${MODE} link: ${PARENT} → ${CHILD}"
echo "============================================"
exec python3 "${PROJECT_DIR}/_init.py" "${PY_ARGS[@]}"
