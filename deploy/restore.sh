#!/usr/bin/env bash
# Restore a backup made by backup.sh into a database.
#
#   restore.sh FILE [--into DBNAME] [--yes-overwrite]
#
# By default the dump goes into a NEW database `home_restore` (created if missing). A target that
# already contains tables is refused unless --yes-overwrite. The live database (`home`, or
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
[[ $TARGET =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || die "invalid database name (use lowercase letters, digits, underscore)"
[[ -f $FILE ]] || die "no such file: $FILE"
gzip -t "$FILE" 2>/dev/null || die "$FILE is not a valid gzip file; nothing was changed"

if [[ $TARGET == home || $TARGET == "$LIVE_DB" ]]; then
  if ((EXPLICIT_INTO == 0 || OVERWRITE == 0)); then
    die "refusing to restore into the live database '$TARGET' without both --into $TARGET and --yes-overwrite"
  fi
fi

psql_admin() { psql -X -w -q -v ON_ERROR_STOP=1 -d postgres "$@"; }
psql_db() { psql -X -w -q -v ON_ERROR_STOP=1 -d "$TARGET" "$@"; }
TABLES_SQL="FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"

exists=$(psql_admin -Atc "SELECT 1 FROM pg_database WHERE datname = '$TARGET'" || die "cannot connect to the database server")
if [[ $exists != 1 ]]; then
  psql_admin -c "CREATE DATABASE \"$TARGET\"" || die "could not create database $TARGET"
  echo "restore: created database $TARGET"
else
  existing=$(psql_db -Atc "SELECT count(*) $TABLES_SQL")
  if ((existing > 0)); then
    ((OVERWRITE == 1)) || die "database $TARGET already contains $existing tables; use a new name or pass --yes-overwrite"
    psql_db -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" || die "could not clear $TARGET"
    echo "restore: cleared the existing tables in $TARGET"
  fi
fi

echo "restore: loading $FILE into $TARGET"
gzip -dc "$FILE" | psql_db -1 -f - >/dev/null || die "loading the dump failed (database $TARGET may be incomplete; drop it and retry)"

tables=$(psql_db -Atc "SELECT count(*) $TABLES_SQL")
expected=$(gzip -dc "$FILE" | grep -c '^CREATE TABLE ' || true)
((tables > 0)) || die "restore finished but $TARGET has no tables"
((tables == expected)) || echo "restore: warning: the dump creates $expected tables but $TARGET has $tables" >&2
echo "restore: database $TARGET restored"
echo "tables: $tables"
while IFS= read -r t; do
  [[ -n $t ]] || continue
  echo "$t: $(psql_db -Atc "SELECT count(*) FROM \"$t\"")"
done < <(psql_db -Atc "SELECT table_name $TABLES_SQL ORDER BY table_name LIMIT 10")
