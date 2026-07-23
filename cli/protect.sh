#!/bin/bash
# protect.sh — Protect a file (make read-only) via cagingcli with --await
#
# Usage:
#   protect.sh <file-path>
#   protect.sh -t <topic> <file-path>
#
# Topic defaults to "na" unless CAGING_TOPIC env var is set.
# -t flag overrides both.
#
# Examples:
#   protect.sh /data/db.sqlite
#   protect.sh -t security /etc/shadow

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOPIC="${CAGING_TOPIC:-na}"
ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -t)  TOPIC="$2"; shift 2 ;;
        -t*) TOPIC="${1#-t}"; shift ;;
        --topic=*) TOPIC="${1#*=}"; shift ;;
        --topic) TOPIC="$2"; shift 2 ;;
        *)   ARGS+=("$1"); shift ;;
    esac
done

exec "${SCRIPT_DIR}/cagingcli.py" -c "${SCRIPT_DIR}/cagingcli.yaml" -t "${TOPIC}" protect "${ARGS[@]}" --await
