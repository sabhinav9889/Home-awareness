#!/usr/bin/env bash
# Safe on-VM deploy with rollback. Run from anywhere; it works in the repository it lives in.
#
#   deploy/deploy.sh [GIT_REF]     GIT_REF: 40-hex sha, origin/main (default) or a branch name
#
# Order: lock, fetch, fast-forward check, checkout, build, migrate, up --wait, health check.
# A failure after the checkout rolls back to the last healthy commit (deploy/.last_good) and
# still exits non-zero. Environment values are never printed; deploy/.env.prod is only read
# for DOMAIN and is never sourced, echoed or passed through a command that could dump it.
#
# Tunables (all optional): HEALTH_URL (default https://$DOMAIN/health), HEALTH_TIMEOUT (90 s),
# HEALTH_INTERVAL (3 s), CURL_EXTRA (extra curl options, e.g. -k for a local self-signed run;
# there is no insecure default).
set -euo pipefail

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
    echo "deploy: invalid GIT_REF (use a 40-hex sha, origin/main or a plain branch name)" >&2
    return 2
  fi
  local health_timeout=${HEALTH_TIMEOUT:-90} health_interval=${HEALTH_INTERVAL:-3}
  [[ $health_timeout =~ ^[0-9]+$ && $health_interval =~ ^[0-9]+$ ]] \
    || { echo "deploy: HEALTH_TIMEOUT and HEALTH_INTERVAL must be whole seconds" >&2; return 2; }

  # --- one deploy at a time ---
  exec 9>"$lock_file"
  if ! flock -n 9; then
    echo "deploy: another deploy is already running; refusing to start a second one" >&2
    return 1
  fi

  # --- configuration (DOMAIN only; the file is never sourced or printed) ---
  if [[ ! -r $env_file ]]; then
    echo "deploy: $env_file is missing or unreadable (create it with scripts/gen_secrets.py)" >&2
    return 1
  fi
  local domain
  domain=$(sed -n 's/^DOMAIN=//p' "$env_file" | tail -n 1 | tr -d '\r"'"'"' ')
  if [[ ! $domain =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
    echo "deploy: DOMAIN is not set to a valid hostname in $env_file" >&2
    return 1
  fi
  local health_url=${HEALTH_URL:-https://$domain/health}
  local -a curl_extra=()
  read -r -a curl_extra <<<"${CURL_EXTRA:-}"

  dc() {
    docker compose -f docker-compose.yml -f docker-compose.prod.yml \
      --env-file "$env_file" -p home-awareness "$@"
  }

  healthy() {
    local deadline=$((SECONDS + health_timeout))
    while true; do
      if curl -fsS --max-time 5 ${curl_extra[@]+"${curl_extra[@]}"} "$health_url" >/dev/null; then
        return 0
      fi
      ((SECONDS >= deadline)) && return 1
      sleep "$health_interval"
    done
  }

  migration_files() { # sorted migration file names at a commit
    git ls-tree -r --name-only "$1" -- migrations/versions | grep -E '\.py$' | sort || true
  }

  # --- resolve the target ---
  git fetch --quiet origin
  local target prev orig_ref
  if [[ $ref =~ $sha_re ]]; then
    target=$(git rev-parse --verify --quiet "${ref}^{commit}") || target=""
  else
    target=$(git rev-parse --verify --quiet "origin/${ref#origin/}^{commit}") || target=""
  fi
  if [[ -z $target ]]; then
    echo "deploy: cannot resolve $ref to a commit after fetching origin" >&2
    return 1
  fi
  prev=$(git rev-parse HEAD)
  orig_ref=$(git symbolic-ref -q --short HEAD || echo "$prev")
  if ! git merge-base --is-ancestor "$prev" "$target"; then
    echo "deploy: $target is not a fast-forward of the checked-out $prev; refusing" >&2
    return 1
  fi

  local last_good=""
  if [[ -r $last_good_file ]]; then
    last_good=$(tr -d '[:space:]' <"$last_good_file")
    [[ $last_good =~ $sha_re ]] || last_good=""
  fi

  echo "deploy: $prev -> $target (last good: ${last_good:-none, first deploy})"
  git checkout --quiet --detach "$target"

  restore_tree() { git checkout --quiet "$orig_ref" || true; }

  # --- build and migrate: failing here leaves the running version untouched ---
  if ! dc build; then
    restore_tree
    echo "deploy: FAILED at build; nothing was changed, the old version keeps running" >&2
    return 1
  fi
  if ! dc run --rm migrate; then
    restore_tree
    echo "deploy: FAILED at migrate; the old version keeps running (check the migration output)" >&2
    return 1
  fi

  # --- switch ---
  local ok=1
  if dc up -d --wait --wait-timeout 120 && healthy; then
    ok=0
  fi
  if ((ok == 0)); then
    printf '%s\n' "$target" >"$last_good_file.tmp"
    mv "$last_good_file.tmp" "$last_good_file"
    docker image prune -f || true # dangling images only; the host may run other projects
    echo "deploy: OK, now running $target"
    return 0
  fi

  # --- the new version is not healthy ---
  echo "deploy: new version $target failed its start or health check" >&2
  if [[ -z $last_good ]]; then
    echo "deploy: FIRST DEPLOY FAILED, there is no previous good version to roll back to." >&2
    echo "deploy: stopping the new containers; fix the cause and deploy again." >&2
    dc stop || true
    return 1
  fi

  local newer
  newer=$(comm -13 <(migration_files "$last_good") <(migration_files "$target") || true)
  if [[ -n $newer ]]; then
    echo "deploy: WARNING: migrations in $target are not in $last_good:" >&2
    echo "$newer" | sed 's/^/deploy:   /' >&2
    echo "deploy: WARNING: production migrations are forward-only. The database may now be" >&2
    echo "deploy: WARNING: newer than the rolled-back code; it is NOT downgraded automatically." >&2
  fi
  echo "deploy: rolling back to $last_good" >&2
  if git checkout --quiet --detach "$last_good" && dc build && dc up -d --wait --wait-timeout 120 \
    && healthy; then
    docker image prune -f || true
    echo "ROLLED BACK to $last_good" >&2
    echo "deploy: the deploy of $target FAILED (the old version is serving again)" >&2
  else
    echo "ROLLBACK FAILED, manual action needed" >&2
  fi
  return 1
}

main "$@"
exit $?
