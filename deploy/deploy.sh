#!/usr/bin/env bash
# Safe on-VM deploy with rollback. Run from anywhere; it works in the repository it lives in.
#
#   deploy/deploy.sh [GIT_REF]     GIT_REF: 40-hex sha, origin/main (default) or a branch name
#
# Order: lock, checks, fetch, fast-forward check, checkout, build, migrate, up --wait, health
# check. A failure after the checkout rolls back to the last healthy commit (deploy/.last_good)
# WITHOUT running migrate (the database may already be newer) and still exits non-zero.
# Environment values are never printed; deploy/.env.prod is only read for DOMAIN and is never
# sourced, echoed or passed through a command that could dump it.
#
# Tunables (all optional): HEALTH_URL (default https://$DOMAIN/health, resolved to 127.0.0.1
# so it works before public DNS), HEALTH_TIMEOUT (90 s), HEALTH_INTERVAL (3 s), CURL_EXTRA
# (extra curl options, e.g. -k for a local self-signed run; there is no insecure default).
set -euo pipefail
export LC_ALL=C
# A dead SSH session (SIGHUP, or a closed stdout giving SIGPIPE) must not kill a deploy
# half-way through the switch. Output helpers below tolerate a closed stdout/stderr.
trap '' PIPE HUP

say() { printf '%s\n' "$*" || true; }
warn() { printf '%s\n' "$*" >&2 || true; }

main() {
  cd "$(dirname "${BASH_SOURCE[0]}")/.."

  local env_file=deploy/.env.prod
  local last_good_file=deploy/.last_good
  local lock_file=deploy/.deploy.lock
  local ref=${1-origin/main}

  # --- argument validation (before anything touches the repo or Docker) ---
  local sha_re='^[0-9a-f]{40}$'
  local branch_re='^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$'
  if ! { [[ $ref =~ $sha_re ]] || [[ $ref =~ $branch_re ]]; } \
    || [[ $ref == *..* || $ref == */ || $ref == *.lock ]]; then
    warn "deploy: invalid GIT_REF (use a 40-hex sha, origin/main or a plain branch name)"
    return 2
  fi
  local health_timeout=${HEALTH_TIMEOUT:-90} health_interval=${HEALTH_INTERVAL:-3}
  [[ $health_timeout =~ ^[0-9]+$ && $health_interval =~ ^[0-9]+$ ]] \
    || { warn "deploy: HEALTH_TIMEOUT and HEALTH_INTERVAL must be whole seconds"; return 2; }

  # --- one deploy at a time ---
  exec 9>"$lock_file"
  if ! flock -n 9; then
    warn "deploy: another deploy is already running; refusing to start a second one"
    return 1
  fi

  # --- configuration (DOMAIN only; the file is never sourced or printed) ---
  if [[ ! -r $env_file ]]; then
    warn "deploy: $env_file is missing or unreadable (create it with scripts/gen_secrets.py)"
    return 1
  fi
  local domain
  domain=$(sed -n 's/^DOMAIN=//p' "$env_file" | tail -n 1 | tr -d '\r"'"'"' ')
  if [[ ! $domain =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
    warn "deploy: DOMAIN is not set to a valid hostname in $env_file"
    return 1
  fi
  local health_url=${HEALTH_URL:-}
  local -a curl_extra=()
  if [[ -z $health_url ]]; then
    health_url=https://$domain/health
    curl_extra=(--resolve "$domain:443:127.0.0.1") # local: works before DNS, keeps SNI and cert
  fi
  local -a curl_user=()
  read -r -a curl_user <<<"${CURL_EXTRA:-}"

  dc() {
    docker compose -f docker-compose.yml -f docker-compose.prod.yml \
      --env-file "$env_file" -p home-awareness "$@"
  }

  # `!reset`/`!override` in the prod overlay and `--wait-timeout` need Compose 2.24+.
  local cver
  cver=$(docker compose version --short 2>/dev/null | head -n 1 | tr -d 'v\r ') || cver=""
  if [[ ! $cver =~ ^[0-9]+\.[0-9]+ ]] \
    || [[ $(printf '%s\n2.24.0\n' "$cver" | sort -V | head -n 1) != 2.24.0 ]]; then
    warn "deploy: Docker Compose 2.24 or newer is required (found: ${cver:-none})"
    return 1
  fi

  health_once() {
    curl -fsS --max-time 5 "${curl_extra[@]}" ${curl_user[@]+"${curl_user[@]}"} "$health_url" \
      >/dev/null
  }
  healthy() {
    local deadline=$((SECONDS + health_timeout))
    while true; do
      health_once && return 0
      ((SECONDS >= deadline)) && return 1
      sleep "$health_interval"
    done
  }

  # Probe with certificate checks off: tells "the app is up but the certificate is not trusted"
  # (ACME staging, or still being issued) apart from "the app is down".
  tls_untrusted() {
    curl -fsS --max-time 5 -k "${curl_extra[@]}" ${curl_user[@]+"${curl_user[@]}"} "$health_url" \
      >/dev/null 2>&1
  }
  tls_note() {
    warn "deploy: the app is up but its TLS certificate is not trusted (ACME staging, or the"
    warn "deploy: certificate is still being issued). Check ACME_CA in deploy/.env.prod, then see"
    warn "deploy: docs/deploy-oracle.md (staging first, then the production CA)."
  }

  migration_files() { # sorted migration file names at a commit
    git ls-tree -r --name-only "$1" -- migrations/versions | grep -E '\.py$' | sort || true
  }

  # --- the checkout must be clean (tracked files) ---
  if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
    warn "deploy: the checkout has local modifications to tracked files; refusing to deploy"
    warn "deploy: commit, stash or discard them on the VM first (git status shows which)"
    return 1
  fi

  # --- resolve the target ---
  # gc.autoDetach=false: a detached gc would inherit the lock fd and hold the lock.
  if ! git -c gc.autoDetach=false fetch --quiet origin; then
    warn "deploy: git fetch from origin failed; nothing was changed"
    return 1
  fi
  local target prev orig_ref
  if [[ $ref =~ $sha_re ]]; then
    target=$(git rev-parse --verify --quiet "${ref}^{commit}") || target=""
  else
    target=$(git rev-parse --verify --quiet "origin/${ref#origin/}^{commit}") || target=""
  fi
  if [[ -z $target ]]; then
    warn "deploy: cannot resolve $ref to a commit after fetching origin"
    return 1
  fi
  prev=$(git rev-parse HEAD)
  orig_ref=$(git symbolic-ref -q --short HEAD || echo "$prev")

  local last_good=""
  if [[ -r $last_good_file ]]; then
    last_good=$(tr -d '[:space:]' <"$last_good_file")
    [[ $last_good =~ $sha_re ]] || last_good=""
  fi

  if [[ $target != "$prev" ]] && git merge-base --is-ancestor "$target" "$prev" \
    && [[ $prev == "$last_good" ]]; then
    say "deploy: already at a newer commit, nothing to do (running $prev)"
    return 0
  fi
  if ! git merge-base --is-ancestor "$prev" "$target"; then
    warn "deploy: $target is not a fast-forward of the checked-out $prev; refusing"
    return 1
  fi

  # After a manual bootstrap there is no .last_good. If the stack is healthy right now, the
  # current checkout is the rollback target (so a failed first automatic deploy rolls back).
  if [[ -z $last_good ]]; then
    if health_once; then
      last_good=$prev
      say "deploy: the running stack is healthy; $prev is the rollback target"
    elif tls_untrusted; then
      tls_note
      warn "deploy: refusing to deploy; nothing was changed"
      return 1
    fi
  fi

  say "deploy: $prev -> $target (rollback target: ${last_good:-none, first deploy})"
  git checkout --quiet --detach "$target"

  restore_tree() { git checkout --quiet "$orig_ref" || true; }
  tags_note() {
    warn "deploy: the local image tags may now point at the new code; a manual 'up -d' without"
    warn "deploy: --build would start it. Re-run the deploy (or rebuild) instead."
  }

  # --- build and migrate: failing here leaves the running version untouched ---
  if ! dc build --pull; then
    restore_tree
    warn "deploy: FAILED at build; nothing was changed, the old version keeps running"
    tags_note
    return 1
  fi
  if ! dc run --rm migrate; then
    restore_tree
    warn "deploy: FAILED at migrate; the old version keeps running (check the migration output)"
    tags_note
    return 1
  fi

  # Single-file bind mounts (Caddyfile, backup/restore/duckdns scripts) are replaced by git with
  # a new inode, so a running container keeps the old file and compose does not recreate it.
  # Recreate the affected services when their file changed (known deploy history only).
  local -a recreate=()
  if [[ -n $last_good && $prev != "$target" ]]; then
    local changed enabled
    changed=$(git diff --name-only "$prev" "$target" || true)
    enabled=$(dc config --services 2>/dev/null || true)
    add_recreate() { # service, files that feed it
      local svc=$1 f
      shift
      grep -qx "$svc" <<<"$enabled" || return 0
      for f in "$@"; do
        if grep -qx "$f" <<<"$changed"; then
          recreate+=("$svc")
          return 0
        fi
      done
    }
    add_recreate caddy deploy/Caddyfile
    add_recreate backup deploy/backup.sh deploy/restore.sh
    add_recreate duckdns deploy/duckdns.sh
  fi

  # --- switch ---
  local up_ok=1
  if dc up -d --wait --wait-timeout 120; then
    up_ok=0
    if ((${#recreate[@]} > 0)); then
      say "deploy: recreating services whose mounted file changed: ${recreate[*]}"
      dc up -d --force-recreate --no-deps "${recreate[@]}" || up_ok=1
    fi
  fi
  if ((up_ok == 0)) && healthy; then
    printf '%s\n' "$target" >"$last_good_file.tmp"
    mv "$last_good_file.tmp" "$last_good_file"
    docker image prune -f || true # dangling images only; the host may run other projects
    say "deploy: OK, now running $target"
    return 0
  fi

  # --- the new version is not healthy ---
  if ((up_ok == 0)) && tls_untrusted; then
    # Up, but not trusted: not a broken release. Leave everything running; never stop or roll
    # back a site that only has the wrong certificate.
    warn "deploy: $target is running but the deploy is NOT marked good"
    tls_note
    return 1
  fi
  warn "deploy: new version $target failed its start or health check"
  if [[ -z $last_good ]]; then
    warn "deploy: FIRST DEPLOY FAILED, there is no previous good version to roll back to."
    warn "deploy: stopping the new api, worker and beat (db, redis and caddy keep running);"
    warn "deploy: fix the cause and deploy again."
    dc stop api worker beat || true
    return 1
  fi

  local newer line
  newer=$(comm -13 <(migration_files "$last_good") <(migration_files "$target") || true)
  if [[ -n $newer ]]; then
    warn "deploy: WARNING: migrations in $target are not in $last_good:"
    while IFS= read -r line; do warn "deploy:   $line"; done <<<"$newer"
    warn "deploy: WARNING: production migrations are forward-only. The database may now be"
    warn "deploy: WARNING: newer than the rolled-back code; it is NOT downgraded automatically."
  fi
  warn "deploy: rolling back to $last_good (migrate is skipped: the database is not downgraded)"
  if git checkout --quiet --detach "$last_good" && dc build api worker beat \
    && dc up -d --no-deps --wait --wait-timeout 120 api worker beat && healthy; then
    docker image prune -f || true
    warn "ROLLED BACK to $last_good"
    warn "deploy: the deploy of $target FAILED (the old version is serving again)"
  else
    warn "ROLLBACK FAILED, manual action needed"
    warn "deploy: checked out now: $(git rev-parse HEAD 2>/dev/null || echo unknown)"
    warn "deploy: containers (name and status only):"
    dc ps --format '{{.Name}} {{.Status}}' 2>&1 | sed 's/^/deploy:   /' >&2 || true
  fi
  return 1
}

main "$@"
exit $?
