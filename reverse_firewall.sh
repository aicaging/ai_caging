#!/usr/bin/env bash
#
# reverse_firewall.sh
#
# User outbound firewall with whitelist management
#
# Commands:
#
#   enable <user> [items...]
#   disable <user>
#   add     <user> <items...>
#   remove  <user> <items...>
#   list    <user>
#
# Items:
#   IP
#   CIDR
#   Domain
#

set -euo pipefail


BASE_DIR="/etc/reverse_firewall"


usage()
{
cat <<EOF

Usage:

 Enable firewall:
   $0 enable <user> [IP/domain...]

 Disable firewall:
   $0 disable <user>

 Add whitelist:
   $0 add <user> IP/domain...

 Remove whitelist:
   $0 remove <user> IP/domain...

 Show whitelist:
   $0 list <user>

EOF
exit 1
}


[[ $EUID -eq 0 ]] || {
    echo "Run as root"
    exit 1
}


[[ $# -ge 2 ]] || usage


ACTION="$1"
USER="$2"

shift 2


id "$USER" >/dev/null 2>&1 || {
    echo "User $USER does not exist"
    exit 1
}


UID_NUM=$(id -u "$USER")

CHAIN="USER_${UID_NUM}_OUT"

mkdir -p "$BASE_DIR"

LIST_FILE="$BASE_DIR/$USER.list"



#######################################
# Resolve domain
#######################################

resolve()
{
    getent ahostsv4 "$1" \
        | awk '{print $1}' \
        | sort -u
}



#######################################
# Enable firewall
#######################################

enable_firewall()
{

    iptables -N "$CHAIN" 2>/dev/null || true
    iptables -F "$CHAIN"


    # localhost
    iptables -A "$CHAIN" \
        -d 127.0.0.0/8 \
        -j ACCEPT


    declare -A DONE


    while read -r ITEM
    do
        [[ -z "$ITEM" ]] && continue
        [[ "$ITEM" =~ ^# ]] && continue


        if [[ "$ITEM" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]+)?$ ]]
        then
            IPS="$ITEM"
        else
            IPS=$(resolve "$ITEM")
        fi


        for IP in $IPS
        do
            if [[ -z "${DONE[$IP]:-}" ]]
            then
                echo "Allow $IP"
                iptables -A "$CHAIN" \
                    -d "$IP" \
                    -j ACCEPT

                DONE[$IP]=1
            fi
        done


    done < "$LIST_FILE"



    iptables -A "$CHAIN" -j REJECT



    while iptables -D OUTPUT \
        -m owner --uid-owner "$UID_NUM" \
        -j "$CHAIN" 2>/dev/null
    do
        :
    done


    iptables -A OUTPUT \
        -m owner --uid-owner "$UID_NUM" \
        -j "$CHAIN"


    echo "Firewall enabled for $USER"
}



#######################################
# Disable firewall
#######################################

disable_firewall()
{

    while iptables -D OUTPUT \
        -m owner --uid-owner "$UID_NUM" \
        -j "$CHAIN" 2>/dev/null
    do
        :
    done


    iptables -F "$CHAIN" 2>/dev/null || true
    iptables -X "$CHAIN" 2>/dev/null || true


    echo "Firewall disabled for $USER"
}



#######################################
# Add whitelist
#######################################

add_items()
{
    touch "$LIST_FILE"

    for ITEM in "$@"
    do
        grep -qxF "$ITEM" "$LIST_FILE" || \
            echo "$ITEM" >> "$LIST_FILE"
    done

    enable_firewall
}



#######################################
# Remove whitelist
#######################################

remove_items()
{
    touch "$LIST_FILE"

    for ITEM in "$@"
    do
        sed -i "\|^$ITEM$|d" "$LIST_FILE"
    done

    enable_firewall
}



#######################################
# List
#######################################

list_items()
{
    echo "Whitelist for $USER:"
    cat "$LIST_FILE" 2>/dev/null || echo "(empty)"
}



#######################################
# Main
#######################################

case "$ACTION" in

enable)

    touch "$LIST_FILE"

    printf "%s\n" "$@" >> "$LIST_FILE"

    sort -u "$LIST_FILE" -o "$LIST_FILE"

    enable_firewall
    ;;


disable)
    disable_firewall
    ;;


add)
    add_items "$@"
    ;;


remove)
    remove_items "$@"
    ;;


list)
    list_items
    ;;


*)
    usage
    ;;

esac