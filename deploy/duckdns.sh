#!/bin/sh
# DuckDNS updater, run by the `duckdns` compose service (curlimages/curl, POSIX sh).
# Every DUCKDNS_INTERVAL seconds (default 300) it asks DuckDNS to point the subdomain at the
# caller's public address (an empty ip= lets DuckDNS use the source address of the request).
#
# Environment: DOMAIN (the subdomain is DOMAIN minus .duckdns.org), DUCKDNS_SUBDOMAIN (overrides
# that), DUCKDNS_TOKEN. Test hooks: DUCKDNS_INTERVAL, DUCKDNS_MAX_LOOPS (0 = run forever).
#
# The token is only ever handed to curl on stdin as a config file (`curl -K -`), so it is not in
# the process arguments, and curl's stderr is discarded and the URL and response body are never
# printed. One log line is written per state change (OK or KO), plus the first result.
#
# A configuration error never makes the container exit: `up -d --wait` in a deploy would see a
# restart loop and fail (and roll back a healthy release). Instead the message is logged and the
# script idles, repeating the message every DUCKDNS_IDLE_INTERVAL seconds (default 3600), and
# exits 0 on TERM/INT. curl -4 keeps an empty ip= from registering an IPv6 address.
# Test hooks: DUCKDNS_IDLE_INTERVAL, DUCKDNS_IDLE_MAX_LOOPS (0 = idle forever).
set -eu

fail() { # log once, then idle forever (exit 0 on TERM)
  idle_interval=${DUCKDNS_IDLE_INTERVAL:-3600}
  idle_max=${DUCKDNS_IDLE_MAX_LOOPS:-0}
  pid=
  trap '[ -z "$pid" ] || kill "$pid" 2>/dev/null; exit 0' TERM INT
  n=0
  while :; do
    printf 'duckdns: %s (the updater is idle until this is fixed and the service is recreated)\n' "$*" >&2
    n=$((n + 1))
    if [ "$idle_max" -gt 0 ] && [ "$n" -ge "$idle_max" ]; then exit 0; fi
    sleep "$idle_interval" >/dev/null 2>&1 &
    pid=$!
    wait "$pid" || true
  done
}

domain=${DOMAIN:-}
token=${DUCKDNS_TOKEN:-}
sub=${DUCKDNS_SUBDOMAIN:-}
interval=${DUCKDNS_INTERVAL:-300}
max_loops=${DUCKDNS_MAX_LOOPS:-0}

if [ -z "$sub" ]; then
  case $domain in
    *.duckdns.org) sub=${domain%.duckdns.org} ;;
    *) fail "DOMAIN does not end in .duckdns.org; set DUCKDNS_SUBDOMAIN or do not enable this service" ;;
  esac
fi
case $sub in
  '' | *[!A-Za-z0-9-]*) fail "the subdomain must be non-empty letters, digits and hyphens only (one label)" ;;
esac
[ -n "$token" ] || fail "DUCKDNS_TOKEN is not set in deploy/.env.prod"
case $token in
  *[!A-Za-z0-9-]*) fail "DUCKDNS_TOKEN has unexpected characters (a DuckDNS token is letters, digits and hyphens)" ;;
esac
case $interval$max_loops in
  *[!0-9]* | '') fail "DUCKDNS_INTERVAL and DUCKDNS_MAX_LOOPS must be whole numbers" ;;
esac

update() { # prints OK or KO; never the URL, the token or the response
  resp=$(printf 'url = "https://www.duckdns.org/update?domains=%s&token=%s&ip="\n' "$sub" "$token" \
    | curl -4 --silent --max-time 30 -K - 2>/dev/null) || resp=""
  if [ "$resp" = "OK" ]; then echo OK; else echo KO; fi
}

state=""
loops=0
while :; do
  new=$(update)
  if [ "$new" != "$state" ]; then
    if [ "$new" = OK ]; then
      if [ -z "$state" ]; then
        echo "duckdns: OK, $sub.duckdns.org is updated (checking every ${interval}s, logging changes only)"
      else
        echo "duckdns: OK again, $sub.duckdns.org is updated"
      fi
    else
      echo "duckdns: KO, the update for $sub.duckdns.org failed or was rejected (check DUCKDNS_TOKEN and the subdomain); retrying every ${interval}s"
    fi
    state=$new
  fi
  loops=$((loops + 1))
  if [ "$max_loops" -gt 0 ] && [ "$loops" -ge "$max_loops" ]; then break; fi
  sleep "$interval"
done
