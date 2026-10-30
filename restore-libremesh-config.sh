#!/bin/sh
set -eu

BASE=/root/libremesh-failsafe
SOURCE=${1:-saved}

board=$(ubus call system board 2>/dev/null | jsonfilter -e '@.board_name' 2>/dev/null || true)
[ -n "$board" ] || board=$(cat /tmp/sysinfo/board_name 2>/dev/null || true)
host=$(hostname)

case "$SOURCE" in
    saved|picot0)
        ARCHIVE=$BASE/picot0-config.tar.gz
        CONFIRM=RESTORE-SAVED
        echo "Saved failsafe config will be applied to board '$board', host '$host'."
        echo "It keeps the hostname/IPs of the node it was taken from."
        ;;
    live)
        ARCHIVE=$BASE/live-config.tar.gz
        CONFIRM=RESTORE-LIVE
        ;;
    *)
        echo "Usage: $0 [saved|live]" >&2
        exit 2
        ;;
esac

[ -s "$ARCHIVE" ] || {
    echo "Failsafe archive not found: $ARCHIVE" >&2
    exit 1
}

manifest=$(mktemp)
trap 'rm -f "$manifest"' EXIT HUP INT TERM
tar -tzf "$ARCHIVE" > "$manifest"
while IFS= read -r entry; do
    case "$entry" in
        etc/config/|etc/config/*) ;;
        *)
            echo "Unexpected path in failsafe archive: $entry" >&2
            exit 1
            ;;
    esac
    case "$entry" in
        *../*|/*)
            echo "Unsafe path in failsafe archive: $entry" >&2
            exit 1
            ;;
    esac
done < "$manifest"
for required in network wireless lime-node firewall dhcp; do
    grep -qx "etc/config/$required" "$manifest" || {
        echo "Failsafe archive lacks etc/config/$required" >&2
        exit 1
    }
done

echo "This replaces the device's UCI configuration from: $ARCHIVE"
echo "The current /etc/config will first be saved under $BASE."
printf "Type '%s' to continue: " "$CONFIRM"
IFS= read -r answer
[ "$answer" = "$CONFIRM" ] || {
    echo "Restore cancelled."
    exit 1
}

stamp=$(date +%Y%m%d-%H%M%S)
mkdir -p "$BASE"
tar -czf "$BASE/pre-restore-$stamp.tar.gz" -C / etc/config

for current in /etc/config/*; do
    [ -f "$current" ] || continue
    relative="etc/config/${current##*/}"
    if ! grep -qxF "$relative" "$manifest"; then
        rm -f "$current"
    fi
done
tar -xzf "$ARCHIVE" -C /

lime-config
lime-apply
/etc/init.d/network restart
wifi reload
/etc/init.d/firewall restart
/etc/init.d/dnsmasq restart

echo "Configuration restored. If network access is lost, use local/serial console."
