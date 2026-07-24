#!/bin/bash
# firewallcli.sh — Manage reverse firewall for cage users via exec.sh
#
# Commands:
#   enable  <user> [IP/domain/CIDR...]    Block all outbound except whitelist
#   disable <user>                         Remove all firewall rules for user
#   add     <user> <IP/domain/CIDR...>     Add entries to whitelist
#   list    <user>                         Show current whitelist
#
# Usage:
#   firewallcli.sh enable cage1 api.deepseek.com
#   firewallcli.sh disable cage1
#   firewallcli.sh add cage1 github.com
#   firewallcli.sh list cage1
#   firewallcli.sh -t security disable cage1
#
# Topic defaults to "na" unless CAGING_TOPIC env var is set.
# -t flag overrides both.

set -euo pipefail

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

# Validate subcommand
SUBCMD="${ARGS[0]:-}"
case "$SUBCMD" in
    enable|disable|add|list) ;;
    *) echo "ERROR: Unknown or missing subcommand. Use: enable|disable|add|list"; exit 1 ;;
esac

REVERSE_FW="${SCRIPT_DIR}/reverse_firewall.sh"

exec "${SCRIPT_DIR}/exec.sh" -t "${TOPIC}" sudo "${REVERSE_FW}" "${ARGS[@]}"
