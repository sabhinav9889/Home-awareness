#!/usr/bin/env bash
# Restore a backup made by backup.sh into a database.
#
#   restore.sh FILE [--into DBNAME] [--yes-overwrite]
#
# By default the dump goes into a NEW database `home_restore` (created if missing). A target that
# already contains tables (in any non-system schema) is refused unless --yes-overwrite. The
# dump is validated first (gzip -t, has CREATE TABLE, has pg_dump's completion trailer), and with
# --yes-overwrite the clearing of the old schemas and the load run in ONE transaction, so a bad
# dump rolls back and leaves the old data intact. postgres/template0/template1 are never targets. The live database (`home`, or
# $PGDATABASE) is only touched with BOTH `--into home` and `--yes-overwrite`; stop the api,
# worker and beat first when you do that. Connection settings come from the PG* variables
# (PGHOST, PGUSER, PGPASSWORD); nothing is printed or passed on a command line.
set -euo pipefail

LIVE_DB=${PGDATABASE:-home}
usage() { echo "usage: restore.sh FILE [--into DBNAME] [--yes-overwrite]" >&2; exit 2; }
die() { echo "restore: $*" >&2; exit 1; }

FILE=""
TARGET=home_restore
OVERWRITE=0
EXPLICIT_INTO=0
while (($#)); do
  case $1 in
    --into) [[ $# -ge 2 ]] || usage; TARGET=$2; EXPLICIT_INTO=1; shift 2 ;;
    --yes-overwrite) OVERWRITE=1; shift ;;
    -*) usage ;;
    *) [[ -z $FILE ]] || usage; FILE=$1; shift ;;
  esac
done
[[ -n $FILE ]] || usage
[[ $TARGET =~ ^[a-z][a-z0-9_]{0,62}$ ]] || die "invalid database name (lowercase letter first, then lowercase letters, digits, underscore; max 63)"
case $TARGET in postgres | template0 | template1) die "refusing to restore into the system database '$TARGET'" ;; esac

if [[ $TARGET == home || $TARGET == "$LIVE_DB" ]]; then
  if ((EXPLICIT_INTO == 0 || OVERWRITE == 0)); then
    die "refusing to restore into the live database '$TARGET' without both --into $TARGET and --yes-overwrite"
  fi
fi

[[ -f $FILE ]] || die "no such file: $FILE"
gzip -t "$FILE" 2>/dev/null || die "$FILE is not a valid gzip file; nothing was changed"
gzip -dc "$FILE" | grep -c '^CREATE TABLE ' >/dev/null || die "$FILE contains no CREATE TABLE; nothing was changed"
gzip -dc "$FILE" | grep -c '^-- PostgreSQL database dump complete' >/dev/null ||
  die "$FILE is truncated (no 'dump complete' trailer); nothing was changed"

psql_admin() { psql -X -w -q -v ON_ERROR_STOP=1 -d postgres "$@"; }
psql_db() { psql -X -w -q -v ON_ERROR_STOP=1 -d "$TARGET" "$@"; }
USER_TABLES="FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema') AND table_schema NOT LIKE 'pg_%' AND table_type = 'BASE TABLE'"

exists=$(psql_admin -Atc "SELECT 1 FROM pg_database WHERE datname = '$TARGET'" || die "cannot connect to the database server")
CLEAR_SQL=""
if [[ $exists != 1 ]]; then
  psql_admin -c "CREATE DATABASE \"$TARGET\"" || die "could not create database $TARGET"
  echo "restore: created database $TARGET"
else
  existing=$(psql_db -Atc "SELECT count(*) $USER_TABLES")
  if ((existing > 0)); then
    ((OVERWRITE == 1)) || die "database $TARGET already contains $existing tables; use a new name or pass --yes-overwrite"
    # Not executed here: it is sent in the same transaction as the load below.
    CLEAR_SQL="DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
    while IFS= read -r stmt; do
      [[ -n $stmt ]] && CLEAR_SQL+=" $stmt"
    done < <(psql_db -Atc "SELECT format('DROP SCHEMA %I CASCADE;', nspname) FROM pg_namespace WHERE nspname NOT IN ('public', 'information_schema') AND nspname NOT LIKE 'pg_%'")
    echo "restore: replacing the $existing existing tables in $TARGET (all-or-nothing)"
  fi
fi

echo "restore: loading $FILE into $TARGET"
{
  [[ -z $CLEAR_SQL ]] || echo "$CLEAR_SQL"
  gzip -dc "$FILE"
} | psql_db -1 -f - >/dev/null || die "loading the dump failed and was rolled back; $TARGET keeps its previous contents"

tables=$(psql_db -Atc "SELECT count(*) $USER_TABLES")
expected=$(gzip -dc "$FILE" | grep -c '^CREATE TABLE ' || true)
((tables > 0)) || die "restore finished but $TARGET has no tables"
((tables == expected)) || echo "restore: warning: the dump creates $expected tables but $TARGET has $tables" >&2
echo "restore: database $TARGET restored"
echo "tables: $tables"
while IFS= read -r t; do
  [[ -n $t ]] || continue
  echo "$t: $(psql_db -Atc "SELECT count(*) FROM $t")"
done < <(psql_db -Atc "SELECT format('%I.%I', table_schema, table_name) $USER_TABLES ORDER BY 1 LIMIT 10")
