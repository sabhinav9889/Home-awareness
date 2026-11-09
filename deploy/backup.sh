#!/usr/bin/env bash
# Nightly logical backup of the `home` database.
#
#   backup.sh            one backup now, then prune old ones
#   backup.sh --daemon   run forever: one backup a day at BACKUP_AT (container clock, UTC), catching up
#                        after downtime (used by the compose `backup` service)
#
# Format: plain SQL (pg_dump --no-owner --no-privileges) gzipped. It restores with plain psql
# anywhere (the local stack, or a hosted Postgres such as Neon) and needs no matching pg_restore.
# Connection settings come from the standard PG* variables (PGHOST, PGUSER, PGDATABASE,
# PGPASSWORD); nothing is ever passed on a command line or printed.
#
# Exit codes: 0 ok, 1 failure (nothing deleted, no partial file left), 3 local backup is fine
# but the optional off-box copy (RCLONE_REMOTE) failed.
set -euo pipefail

BACKUP_DIR=${BACKUP_DIR:-/backups}
MIN_FREE_MB=${MIN_FREE_MB:-1024}
KEEP_DAILY=${KEEP_DAILY:-14}
KEEP_WEEKLY=${KEEP_WEEKLY:-8}
BACKUP_AT=${BACKUP_AT:-0330}
POLL_SECONDS=${POLL_SECONDS:-60}
RETRY_SECONDS=${RETRY_SECONDS:-3600}
SELF=$(readlink -f "${BASH_SOURCE[0]}")

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') backup: $*"; }

# Day of week for a date, 0 = Sunday (Sakamoto). Pure arithmetic: busybox date has no portable -d.
day_of_week() {
  local y=$((10#$1)) m=$((10#$2)) d=$((10#$3))
  local t=(0 3 2 5 0 3 5 1 4 6 2 4)
  if ((m < 3)); then y=$((y - 1)); fi
  echo $(((y + y / 4 - y / 100 + y / 400 + t[m - 1] + d) % 7))
}

# Keep one file per calendar day (the latest) for the newest KEEP_DAILY days; beyond that keep
# the newest KEEP_WEEKLY Sundays; delete everything else that looks like one of our backups.
prune() {
  local newest=$1 f name day days=0 weeklies=0 last=""
  local files=("$BACKUP_DIR"/home-*.sql.gz)
  local i
  for ((i = ${#files[@]} - 1; i >= 0; i--)); do # glob sorts ascending by name = by timestamp
    f=${files[i]}
    [[ -e $f ]] || continue
    name=$(basename "$f")
    [[ $name =~ ^home-([0-9]{8})-[0-9]{6}\.sql\.gz$ ]] || continue
    [[ $f == "$newest" ]] && { last=${BASH_REMATCH[1]}; days=$((days + 1)); continue; }
    day=${BASH_REMATCH[1]}
    if [[ $day == "$last" ]]; then
      rm -f -- "$f" # an older file from a day already represented
      continue
    fi
    last=$day
    days=$((days + 1))
    if ((days <= KEEP_DAILY)); then
      continue
    fi
    if ((weeklies < KEEP_WEEKLY)) &&
      [[ $(day_of_week "${day:0:4}" "${day:4:2}" "${day:6:2}") == 0 ]]; then
      weeklies=$((weeklies + 1))
      continue
    fi
    rm -f -- "$f"
  done
}

free_mb() {
  local kb
  kb=$(df -Pk "$BACKUP_DIR" | awk 'NR==2 {print $4}')
  echo $((kb / 1024))
}

PART=""
cleanup() { [[ -n $PART ]] && rm -f -- "$PART"; return 0; }

backup_once() {
  mkdir -p "$BACKUP_DIR"
  local free
  free=$(free_mb)
  if ((free < MIN_FREE_MB)); then
    log "backup failed: only ${free} MB free space in $BACKUP_DIR, need ${MIN_FREE_MB} MB; nothing written"
    return 1
  fi
  # Parts left by a killed run (older than 2 hours) are garbage.
  find "$BACKUP_DIR" -maxdepth 1 -name 'home-*.sql.gz.part' -mmin +120 -delete 2>/dev/null || true

  local stamp final
  stamp=$(date +%Y%m%d-%H%M%S)
  final="$BACKUP_DIR/home-$stamp.sql.gz"
  PART="$final.part"
  trap cleanup EXIT

  if ! pg_dump --no-owner --no-privileges | gzip -c >"$PART"; then
    log "backup failed: pg_dump did not complete; old backups kept"
    return 1
  fi
  if ! gzip -t "$PART"; then
    log "backup failed: the compressed dump is corrupt; old backups kept"
    return 1
  fi
  if ! gzip -dc "$PART" | grep -c 'CREATE TABLE' >/dev/null; then
    log "backup failed: the dump contains no CREATE TABLE (empty database or wrong target?); old backups kept"
    return 1
  fi
  mv -f -- "$PART" "$final"
  PART=""
  log "wrote $final ($(wc -c <"$final") bytes)"

  prune "$final"

  if [[ -n ${RCLONE_REMOTE:-} ]]; then
    if ! command -v rclone >/dev/null 2>&1; then
      log "warning: RCLONE_REMOTE is set but rclone is not installed; no off-box copy made"
      return 3
    fi
    if ! rclone copy "$final" "$RCLONE_REMOTE"; then
      log "warning: off-box copy to the remote failed; the local backup is intact"
      return 3
    fi
    log "copied to the off-box remote"
  fi
}

daemon() {
  local last_attempt=0 now today hm
  log "daemon started; one backup a day at ${BACKUP_AT:0:2}:${BACKUP_AT:2:2} container time (UTC)"
  trap 'exit 0' TERM INT
  while true; do
    now=$(date +%s)
    today=$(date +%Y%m%d)
    hm=$(date +%H%M)
    if ((10#$hm >= 10#$BACKUP_AT)) && ((now - last_attempt >= RETRY_SECONDS)); then
      if ! compgen -G "$BACKUP_DIR/home-$today-*.sql.gz" >/dev/null; then
        last_attempt=$now
        bash "$SELF" || log "scheduled backup exited with status $? (retry in ${RETRY_SECONDS}s)"
      fi
    fi
    sleep "$POLL_SECONDS" &
    wait $! || true
  done
}

if [[ ${1:-} == "--daemon" ]]; then
  daemon
else
  backup_once
fi
