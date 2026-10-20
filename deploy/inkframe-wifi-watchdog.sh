#!/usr/bin/env bash
# Wi-Fi watchdog for the frame (run by inkframe-wifi-watchdog.timer every
# minute as root).  Pings the default gateway on wlan0; after 3 consecutive
# misses it bounces the radio, after every further 3 it bounces again, and
# after 15 misses (about 15 minutes without a gateway) it reboots.  The
# counter lives in /run so a reboot clears it.  Everything is logged to the
# journal under the tag inkframe-wifi-watchdog.
set -u

IFACE=${INKFRAME_WIFI_IFACE:-wlan0}
STATE=/run/inkframe-wifi-watchdog.failures
BOUNCE_AFTER=3
REBOOT_AFTER=15
TAG=inkframe-wifi-watchdog

note() { logger -t "$TAG" -- "$*"; }

gw=$(ip -4 route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}')
fails=$(cat "$STATE" 2>/dev/null || echo 0)
case "$fails" in *[!0-9]*) fails=0 ;; esac

if [ -n "$gw" ] && ping -c 2 -W 3 -I "$IFACE" "$gw" >/dev/null 2>&1; then
  if [ "$fails" -gt 0 ]; then
    note "gateway $gw reachable again after $fails miss(es)"
  fi
  echo 0 >"$STATE"
  exit 0
fi

fails=$((fails + 1))
echo "$fails" >"$STATE"
note "gateway ${gw:-unknown} unreachable on $IFACE (miss $fails)"

if [ "$fails" -ge "$REBOOT_AFTER" ]; then
  note "no gateway for $fails minutes; rebooting"
  systemctl reboot
  exit 0
fi

if [ "$fails" -ge "$BOUNCE_AFTER" ] && [ $(((fails - BOUNCE_AFTER) % 3)) -eq 0 ]; then
  note "bouncing the Wi-Fi radio"
  nmcli radio wifi off
  sleep 4
  nmcli radio wifi on
fi
exit 0
