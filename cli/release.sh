#!/bin/bash
# release.sh — Release a protected file via cagingcli with --await
#
# Usage:
#   release.sh <file-path>
#   release.sh -t <topic> <file-path>
#   release.sh --reason "done" <file-path>
#
# Topic defaults to "na" unless CAGING_TOPIC env var is set.
# -t flag overrides both.
#
# Examples:
#   release.sh /data/db.sqlite
#   release.sh -t security --reason "backup complete" /etc/shadow

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

exec "${SCRIPT_DIR}/cagingcli.py" -c "${SCRIPT_DIR}/cagingcli.yaml" -t "${TOPIC}" release "${ARGS[@]}" --await
