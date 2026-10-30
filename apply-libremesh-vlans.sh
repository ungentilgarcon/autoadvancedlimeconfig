#!/bin/sh
set -eu

BASE=/root/libremesh-failsafe
SCRIPT=$BASE/apply-libremesh-vlans.sh
PASS_FILE=$BASE/equipment.pass
PENDING=$BASE/vlan-pending
ROLLBACK_SECONDS=180

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

config_value() {
    uci -c "$1" -q get "picot-vlan.settings.$2" 2>/dev/null || true
}

pass_value() {
    name=$1
    pass_file=$2
    awk -F= -v wanted="$name" '
        $1 == wanted {
            sub(/^[^=]*=/, "")
            value = $0
            count++
        }
        END {
            if (count != 1)
                exit 1
            print value
        }
    ' "$pass_file"
}

validate_pass_file() {
    pass_file=$1
    [ -f "$pass_file" ] || fail "private .pass file is required"
    for name in IOT_WIFI_KEY GUEST_WIFI_KEY; do
        key=$(pass_value "$name" "$pass_file") ||
            fail "$name is missing or duplicated in .pass"
        [ "$(printf '%s' "$key" | wc -c | tr -d ' ')" -ge 8 ] &&
            [ "$(printf '%s' "$key" | wc -c | tr -d ' ')" -le 63 ] ||
            fail "$name must contain 8 to 63 characters"
        if LC_ALL=C printf '%s' "$key" | grep -q '[^ -~]'; then
            fail "$name must be printable ASCII"
        fi
    done
}

validate_config() {
    config_dir=$1
    enabled=$(config_value "$config_dir" enabled)
    role=$(config_value "$config_dir" role)
    [ "$enabled" = 0 ] || [ "$enabled" = 1 ] ||
        fail "enabled must be 0 or 1"
    [ "$role" = gateway ] || [ "$role" = edge ] ||
        fail "role must be gateway or edge"

    [ "$enabled" = 1 ] || return 0

    radios=$(config_value "$config_dir" radios)
    [ -n "$radios" ] || fail "enabled config must list radios"
    if printf '%s' "$radios" | grep -q '[^A-Za-z0-9_ ]'; then
        fail "radios may contain only device names separated by spaces"
    fi
    for radio in $radios; do
        case "$radio" in
            ''|*[!A-Za-z0-9_]*)
                fail "invalid radio name"
                ;;
        esac
        uci -q get "wireless.$radio.band" >/dev/null 2>&1 ||
            fail "configured radio '$radio' is not present"
    done
    [ "$(printf '%s\n' "$radios" | wc -w | tr -d ' ')" -eq \
      "$(printf '%s\n' "$radios" | tr ' ' '\n' | sort -u | wc -l | tr -d ' ')" ] ||
        fail "radios must not be repeated"

    [ "$(uci -q get network.bat0.proto 2>/dev/null || true)" = batadv ] ||
        fail "network.bat0 is not configured as a batman-adv interface"
    ip link show bat0 >/dev/null 2>&1 ||
        fail "bat0 is not present; check mesh state before applying"

    if [ "$role" = gateway ]; then
        [ "$(hostname)" = PICOT0 ] ||
            fail "gateway role is restricted to hostname PICOT0"
        board=$(ubus call system board 2>/dev/null |
            jsonfilter -e '@.board_name' 2>/dev/null || true)
        [ -n "$board" ] || board=$(cat /tmp/sysinfo/board_name 2>/dev/null || true)
        [ "$board" = xiaomi,redmi-router-ax6000-ubootmod ] ||
            fail "gateway role is restricted to the Redmi AX6000 U-BootMod"
    elif [ "$(hostname)" = PICOT0 ]; then
        fail "PICOT0 must use the gateway role"
    fi

    for network in iot guest; do
        ssid=$(config_value "$config_dir" "${network}_ssid")
        [ -n "$ssid" ] || fail "${network}_ssid must not be empty"
        [ "$(printf '%s' "$ssid" | wc -c | tr -d ' ')" -le 32 ] ||
            fail "${network}_ssid exceeds the 32-byte Wi-Fi limit"
        if LC_ALL=C printf '%s' "$ssid" | grep -q '[^ -~]'; then
            fail "${network}_ssid must be printable ASCII"
        fi
    done
}

restart_services() {
    /etc/init.d/network reload
    wifi reload
    /etc/init.d/firewall restart
    /etc/init.d/dnsmasq restart
}

restore_transaction() {
    transaction=$1
    restart=${2:-yes}
    printf '%s' "$transaction" | grep -Eq '^[0-9]{14}-[0-9]+$' ||
        fail "invalid change ID"
    [ -s "$PENDING" ] || fail "there is no pending VLAN change"
    active=$(cat "$PENDING")
    [ "$active" = "$transaction" ] || fail "change ID does not match pending transaction"
    archive=$BASE/vlan-$transaction/config.tar.gz
    [ -s "$archive" ] || fail "rollback archive is missing: $archive"
    tar -xzf "$archive" -C /
    [ -f "$BASE/vlan-$transaction/had-picot-vlan" ] ||
        rm -f /etc/config/picot-vlan
    if [ -f "$BASE/vlan-$transaction/had-equipment-pass" ]; then
        install -m 600 "$BASE/vlan-$transaction/previous.pass" "$PASS_FILE"
    else
        rm -f "$PASS_FILE"
    fi
    rm -f "$PENDING"
    if [ "$restart" = yes ]; then
        restart_services
    fi
    echo "VLAN change $transaction rolled back."
}

remove_managed_sections() {
    for section in picot_vlan10_device picot_vlan20_device \
        picot_vlan_iot picot_vlan_guest \
        picot_vlan_iot_dhcp picot_vlan_guest_dhcp \
        picot_vlan_iot_dns_udp picot_vlan_iot_dns_tcp \
        picot_vlan_guest_dns_udp picot_vlan_guest_dns_tcp \
        picot_vlan_lan_to_iot picot_vlan_lan_to_guest \
        picot_vlan_guest_to_wan; do
        uci -q delete "network.$section" || true
        uci -q delete "dhcp.$section" || true
        uci -q delete "firewall.$section" || true
    done
    for radio in $(uci -q show wireless 2>/dev/null |
        sed -n "s/^wireless\\.\\(picot_vlan_\\(iot\\|guest\\)_radio[A-Za-z0-9_]*\\)=.*/\\1/p"); do
        uci -q delete "wireless.$radio" || true
    done
}

add_dhcp_zone_rules() {
    network=$1
    zone=$2

    uci set "firewall.picot_vlan_${network}_dns_udp=rule"
    uci set "firewall.picot_vlan_${network}_dns_udp.name=Allow-${zone}-DNS-UDP"
    uci set "firewall.picot_vlan_${network}_dns_udp.src=$zone"
    uci set "firewall.picot_vlan_${network}_dns_udp.proto=udp"
    uci set "firewall.picot_vlan_${network}_dns_udp.dest_port=53"
    uci set "firewall.picot_vlan_${network}_dns_udp.family=ipv4"
    uci set "firewall.picot_vlan_${network}_dns_udp.target=ACCEPT"

    uci set "firewall.picot_vlan_${network}_dns_tcp=rule"
    uci set "firewall.picot_vlan_${network}_dns_tcp.name=Allow-${zone}-DNS-TCP"
    uci set "firewall.picot_vlan_${network}_dns_tcp.src=$zone"
    uci set "firewall.picot_vlan_${network}_dns_tcp.proto=tcp"
    uci set "firewall.picot_vlan_${network}_dns_tcp.dest_port=53"
    uci set "firewall.picot_vlan_${network}_dns_tcp.family=ipv4"
    uci set "firewall.picot_vlan_${network}_dns_tcp.target=ACCEPT"

    uci set "dhcp.picot_vlan_${network}_dhcp=dhcp"
    uci set "dhcp.picot_vlan_${network}_dhcp.interface=picot_vlan_${network}"
    uci set "dhcp.picot_vlan_${network}_dhcp.start=100"
    uci set "dhcp.picot_vlan_${network}_dhcp.limit=150"
    uci set "dhcp.picot_vlan_${network}_dhcp.leasetime=12h"
    uci set "dhcp.picot_vlan_${network}_dhcp.force=1"

    uci set "firewall.picot_vlan_${network}_dhcp=rule"
    uci set "firewall.picot_vlan_${network}_dhcp.name=Allow-${zone}-DHCP"
    uci set "firewall.picot_vlan_${network}_dhcp.src=$zone"
    uci set "firewall.picot_vlan_${network}_dhcp.proto=udp"
    uci set "firewall.picot_vlan_${network}_dhcp.dest_port=67"
    uci set "firewall.picot_vlan_${network}_dhcp.family=ipv4"
    uci set "firewall.picot_vlan_${network}_dhcp.target=ACCEPT"
}

configure_network() {
    role=$1

    remove_managed_sections

    for vid in 10 20; do
        uci set "network.picot_vlan${vid}_device=device"
        uci set "network.picot_vlan${vid}_device.name=bat0.$vid"
        uci set "network.picot_vlan${vid}_device.type=8021q"
        uci set "network.picot_vlan${vid}_device.ifname=bat0"
        uci set "network.picot_vlan${vid}_device.vid=$vid"
    done

    for network in iot guest; do
        if [ "$network" = iot ]; then
            vid=10
            zone=iot
        else
            vid=20
            zone=guest
        fi

        uci set "network.picot_vlan_$network=interface"
        uci set "network.picot_vlan_$network.device=bat0.$vid"
        if [ "$role" = gateway ]; then
            uci set "network.picot_vlan_$network.proto=static"
            uci set "network.picot_vlan_$network.ipaddr=192.168.$vid.1"
            uci set "network.picot_vlan_$network.netmask=255.255.255.0"
        else
            uci set "network.picot_vlan_$network.proto=none"
        fi

        if [ "$role" = gateway ]; then
            uci set "firewall.picot_vlan_$network=zone"
            uci set "firewall.picot_vlan_$network.name=$zone"
            uci add_list "firewall.picot_vlan_$network.network=picot_vlan_$network"
            uci set "firewall.picot_vlan_$network.input=REJECT"
            uci set "firewall.picot_vlan_$network.output=ACCEPT"
            uci set "firewall.picot_vlan_$network.forward=REJECT"
            add_dhcp_zone_rules "$network" "$zone"
        fi
    done

    if [ "$role" = gateway ]; then
        uci set firewall.picot_vlan_lan_to_iot=forwarding
        uci set firewall.picot_vlan_lan_to_iot.src=lan
        uci set firewall.picot_vlan_lan_to_iot.dest=iot
        uci set firewall.picot_vlan_lan_to_guest=forwarding
        uci set firewall.picot_vlan_lan_to_guest.src=lan
        uci set firewall.picot_vlan_lan_to_guest.dest=guest
        uci set firewall.picot_vlan_guest_to_wan=forwarding
        uci set firewall.picot_vlan_guest_to_wan.src=guest
        uci set firewall.picot_vlan_guest_to_wan.dest=wan
    fi

    radios=$(config_value /etc/config radios)
    for radio in $radios; do
        for network in iot guest; do
            section="picot_vlan_${network}_${radio}"
            if [ "$network" = iot ]; then
                key_name=IOT_WIFI_KEY
            else
                key_name=GUEST_WIFI_KEY
            fi
            key=$(pass_value "$key_name" "$PASS_FILE") ||
                fail "$key_name is missing from .pass"
            uci set "wireless.$section=wifi-iface"
            uci set "wireless.$section.device=$radio"
            uci set "wireless.$section.mode=ap"
            uci set "wireless.$section.network=picot_vlan_$network"
            uci set "wireless.$section.ssid=$(config_value /etc/config "${network}_ssid")"
            uci set "wireless.$section.encryption=psk2"
            uci set "wireless.$section.key=$key"
            if [ "$network" = guest ]; then
                uci set "wireless.$section.isolate=1"
            fi
        done
    done

    uci commit network
    uci commit wireless
    uci commit dhcp
    uci commit firewall
}

start_watchdog() {
    transaction=$1
    command -v nohup >/dev/null 2>&1 || fail "nohup is required for timed rollback"
    nohup sh -c '
        sleep "$1"
        [ -s "$2" ] || exit 0
        [ "$(cat "$2")" = "$3" ] || exit 0
        exec "$4" rollback "$3"
    ' sh "$ROLLBACK_SECONDS" "$PENDING" "$transaction" "$SCRIPT" \
        >/dev/null 2>&1 </dev/null &
    watchdog_pid=$!
    sleep 1
    kill -0 "$watchdog_pid" 2>/dev/null ||
        fail "could not start the automatic rollback watchdog"
}

apply_config() {
    source_config=$1
    source_pass=$2
    [ -f "$source_config" ] || fail "config file not found: $source_config"
    [ -f "$source_pass" ] || fail "private .pass file not found"
    [ "$(id -u)" -eq 0 ] || fail "must run as root"
    mkdir -p "$BASE"
    [ ! -e "$PENDING" ] || fail "another VLAN change is still pending"

    temp_dir=$(mktemp -d)
    trap 'rm -rf "$temp_dir"' EXIT HUP INT TERM
    install -m 600 "$source_config" "$temp_dir/picot-vlan"
    validate_config "$temp_dir"
    enabled=$(config_value "$temp_dir" enabled)
    if [ "$enabled" = 1 ]; then
        validate_pass_file "$source_pass"
    fi

    transaction="$(date +%Y%m%d%H%M%S)-$$"
    transaction_dir=$BASE/vlan-$transaction
    mkdir -m 700 "$transaction_dir"
    config_files="etc/config/network etc/config/wireless etc/config/dhcp etc/config/firewall"
    if [ -f /etc/config/picot-vlan ]; then
        config_files="$config_files etc/config/picot-vlan"
        : > "$transaction_dir/had-picot-vlan"
    fi
    tar -czf "$transaction_dir/config.tar.gz" -C / $config_files
    if [ -f "$PASS_FILE" ]; then
        cp -p "$PASS_FILE" "$transaction_dir/previous.pass"
        : > "$transaction_dir/had-equipment-pass"
    fi

    printf '%s\n' "$transaction" > "$PENDING.tmp"
    mv -f "$PENDING.tmp" "$PENDING"
    apply_failed=1
    apply_started=0
    trap 'cleanup_apply $?' EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    start_watchdog "$transaction"
    apply_started=1
    install -m 600 "$temp_dir/picot-vlan" /etc/config/picot-vlan
    install -m 600 "$source_pass" "$PASS_FILE"
    for path in /root/libremesh-failsafe/apply-libremesh-vlans.sh \
        /root/libremesh-failsafe/equipment.pass /etc/config/picot-vlan \
        /etc/init.d/picot-vlan-recovery; do
        grep -qxF "$path" /etc/sysupgrade.conf 2>/dev/null ||
            echo "$path" >> /etc/sysupgrade.conf
    done

    enabled=$(config_value /etc/config enabled)
    if [ "$enabled" = 1 ]; then
        role=$(config_value /etc/config role)
        configure_network "$role"
    else
        remove_managed_sections
        uci commit network
        uci commit wireless
        uci commit dhcp
        uci commit firewall
    fi

    restart_services
    [ -s "$PENDING" ] && [ "$(cat "$PENDING")" = "$transaction" ] ||
        fail "automatic rollback expired before the change completed"
    apply_failed=0
    trap - EXIT HUP INT TERM
    rm -rf "$temp_dir"
    echo "VLAN_CHANGE_ID=$transaction"
    echo "Confirm from a working connection within $ROLLBACK_SECONDS seconds:"
    echo "$SCRIPT confirm $transaction"
}

confirm_change() {
    transaction=$1
    printf '%s' "$transaction" | grep -Eq '^[0-9]{14}-[0-9]+$' ||
        fail "invalid change ID"
    [ -s "$PENDING" ] || fail "there is no pending VLAN change"
    [ "$(cat "$PENDING")" = "$transaction" ] ||
        fail "change ID does not match pending transaction"
    rm -f "$PENDING"
    echo "VLAN change $transaction confirmed."
}

cleanup_apply() {
    status=$1
    trap - EXIT HUP INT TERM
    if [ "$apply_failed" -eq 1 ]; then
        if [ "$apply_started" -eq 1 ] && [ -s "$PENDING" ]; then
            "$SCRIPT" rollback "$transaction" || true
        else
            rm -f "$PENDING"
        fi
    fi
    rm -rf "$temp_dir"
    case "$source_config" in
        /root/libremesh-failsafe/picot-vlan.conf.upload)
            rm -f "$source_config"
            ;;
    esac
    case "$source_pass" in
        /root/libremesh-failsafe/equipment.pass.upload)
            rm -f "$source_pass"
            ;;
    esac
    exit "$status"
}

case "${1:-}" in
    validate)
        [ "$#" -eq 3 ] || fail "Usage: $0 validate CONFIG PASS_FILE"
        temp_dir=$(mktemp -d)
        trap 'rm -rf "$temp_dir"' EXIT HUP INT TERM
        install -m 600 "$2" "$temp_dir/picot-vlan"
        validate_config "$temp_dir"
        if [ "$(config_value "$temp_dir" enabled)" = 1 ]; then
            validate_pass_file "$3"
        fi
        echo "VLAN config is valid; no changes made."
        ;;
    apply)
        [ "$#" -eq 3 ] || fail "Usage: $0 apply CONFIG PASS_FILE"
        apply_config "$2" "$3"
        ;;
    confirm)
        [ "$#" -eq 2 ] || fail "Usage: $0 confirm CHANGE_ID"
        confirm_change "$2"
        ;;
    rollback)
        [ "$#" -eq 2 ] || fail "Usage: $0 rollback CHANGE_ID"
        restore_transaction "$2"
        ;;
    rollback-boot)
        [ "$#" -eq 2 ] || fail "Usage: $0 rollback-boot CHANGE_ID"
        restore_transaction "$2" no
        ;;
    *)
        fail "Usage: $0 {validate CONFIG PASS_FILE|apply CONFIG PASS_FILE|confirm CHANGE_ID|rollback CHANGE_ID}"
        ;;
esac
