#!/bin/bash
# exec.sh — Execute a command via cagingcli with --await polling
#
# Usage:
#   exec.sh <command> [args...]
#   exec.sh -t <topic> <command> [args...]
#
# Topic defaults to "na" unless CAGING_TOPIC env var is set.
# -t flag overrides both.
#
# Examples:
#   exec.sh cat /etc/hostname
#   exec.sh -t deploy ./deploy.sh
#   CAGING_TOPIC=backup exec.sh rsync -av /data/ /backup/

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

exec "${SCRIPT_DIR}/cagingcli.py" -c "${SCRIPT_DIR}/cagingcli.yaml" -t "${TOPIC}" exec "${ARGS[@]}" --await
