#!/usr/bin/env bash
# /bots — example: show systemd-user service status for your agent bots.
#
# This is a TEMPLATE. Edit the SERVICES array to match the names of your
# systemd units. Useful for "is my bot alive" checks from Discord without
# needing to SSH.

set -euo pipefail

# EDIT THIS LIST to match your bot service names.
SERVICES=(
  "my-bot-1"
  "my-bot-2"
)

printf "%-22s %-10s %-10s %s\n" "service" "state" "sub" "since"
printf -- "%.0s-" {1..70}
printf "\n"

for svc in "${SERVICES[@]}"; do
  if systemctl --user list-unit-files "${svc}.service" --no-legend 2>/dev/null | grep -q .; then
    state=$(systemctl --user is-active "${svc}.service" 2>/dev/null || echo "?")
    sub=$(systemctl --user show -p SubState --value "${svc}.service" 2>/dev/null || echo "?")
    since=$(systemctl --user show -p ActiveEnterTimestamp --value "${svc}.service" 2>/dev/null | cut -d' ' -f2,3)
    printf "%-22s %-10s %-10s %s\n" "$svc" "$state" "$sub" "${since:-?}"
  fi
done
