# Deployment

## Backups

The `backup` service (the `postgres:16-alpine` image, only for its `pg_dump`/`psql`/`gzip`)
dumps the `home` database every night at **03:30 container time (UTC, the container has no timezone set)** into the `backups` volume as
`home-YYYYmmdd-HHMMSS.sql.gz`. The schedule is a plain loop in `deploy/backup.sh --daemon` that
checks once a minute (no cron to configure): it runs when it is past 03:30 and there is no backup
from today yet, so a container that was down at 03:30 catches up on start, and a day never gets two
scheduled backups. A failed run is retried after an hour.

- Format: plain SQL, gzipped, `--no-owner --no-privileges`. Plain SQL restores with `psql` into any
  Postgres, including a hosted one (Neon) if you ever move off the VM.
- Written as `.part`, verified (`gzip -t`, contains `CREATE TABLE`), then renamed, so a failed
  or interrupted run never leaves a truncated backup and never deletes older ones.
- Retention: one file per day for the newest 14 days, plus the newest 8 Sunday files older than
  that. The rest are deleted (by the timestamp in the name).
- It refuses to run with less than 1 GB free (`MIN_FREE_MB`) and logs why: `docker compose ... logs backup`.
- Snapshots are **not backed up**, by design: they are short-lived and encrypted. Consequently the
  `SNAPSHOT_KEY` only matters for snapshots that exist right now; losing it makes the stored
  snapshots unreadable. Keep `deploy/.env.prod` (or at least `SNAPSHOT_KEY`, `JWT_SECRET` and
  `POSTGRES_PASSWORD`) in a password manager; the database backup does not contain them.

Run one by hand and list the results:

    docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file deploy/.env.prod \
      -p home-awareness exec -T backup bash /deploy/backup.sh
    docker compose ... exec -T backup ls -l /backups

### Restore (practise this once, into a scratch database)

    docker compose ... exec -T backup bash /deploy/restore.sh /backups/home-20261109-033000.sql.gz

This creates `home_restore`, loads the file, and prints the table count and a few row counts.
It refuses a target that already has tables unless `--yes-overwrite`, and refuses the live `home`
database unless you pass both `--into home --yes-overwrite` (stop `api`, `worker` and `beat`
first; a restore over `home` replaces everything in it). To copy a backup off the VM:
`docker compose ... cp backup:/backups/FILE .`. To load it into another Postgres (for example
Neon): `gunzip -c FILE | psql "$NEON_DATABASE_URL"`.

### Optional off-box copy (Oracle Object Storage, S3 compatible, Always Free)

`backup.sh` copies each new file with `rclone copy` when `RCLONE_REMOTE` is set and `rclone` is on
its PATH; a failed copy logs a warning and the script exits with status 3, and the local backup
stays. The `backup` container does not ship rclone (installing it at start needs network and adds
a moving part to a nightly job), so the supported route is a separate step on the VM:

1. Create a bucket in Oracle Object Storage (10 GB are free) and a Customer Secret Key for your
   user (Profile, Customer secret keys); note the access key, the secret and your namespace.
2. On the VM, `rclone config`: type `s3`, provider `Other`, endpoint
   `https://<namespace>.compat.objectstorage.<region>.oraclecloud.com`, then the keys.
3. Nightly at 04:30, from the VM crontab (after the 03:30 dump):
   `docker run --rm -v home-awareness_backups:/backups:ro -v $HOME/.config/rclone:/config/rclone rclone/rclone:1.68.2 copy /backups oci:<bucket>/home --min-age 1m`
4. Set a bucket lifecycle rule to expire objects after 60 days so the free tier never fills.

Running `backup.sh` directly on a host that has rclone (with `RCLONE_REMOTE=oci:<bucket>/home`)
does the same without the crontab line.

## Notes

- Processes running on the VM itself can reach the api container directly (its address on the
  compose network), bypassing Caddy and its `X-Forwarded-For` overwrite. That is acceptable on a
  single-user VM; do not run untrusted code on it.
- Clients arriving over IPv6 on a host where containers have no IPv6 would all appear with the
  docker gateway address and share one rate-limit bucket. Oracle Always Free VMs have no IPv6 by
  default, so this does not apply unless you enable it.

## Deploys

`deploy/deploy.sh [GIT_REF]` runs on the VM (GitHub Actions calls it over SSH after CI passes on
`main`; you can also run it by hand). One deploy runs at a time (`flock`); a second one is refused.

1. Checks: clean checkout, Docker Compose 2.24+, `deploy/.env.prod` with a `DOMAIN`.
2. Fetches, then requires a fast-forward from the checked-out commit.
3. Builds the images, then runs the migrations as a separate step. If either fails, the old version
   keeps running and the checkout is restored.
4. `up -d --wait`, then checks `https://$DOMAIN/health` (resolved to 127.0.0.1, so it works before
   public DNS) for up to 90 seconds.
5. Only after that passes it records the commit in `deploy/.last_good` and prunes dangling images.

**Rollback.** If the new version fails after the switch, the script checks out `.last_good`, rebuilds
`api`, `worker` and `beat`, restarts only those (`--no-deps`, so migrate is not run), health-checks
again, prints `ROLLED BACK to <sha>` and still exits non-zero (the deploy failed). If the rollback is
unhealthy too it prints `ROLLBACK FAILED, manual action needed` with the checked-out commit and the
container states. On the very first deploy with no `.last_good`, the running stack counts as good if
it is healthy before the switch; if it was not, only api, worker and beat are stopped.

**Limits.** Migrations are forward-only in production: a rollback never downgrades the database, and
the script warns when the failed commit added migrations. Write migrations additively (add columns
and tables first, drop or rename only in a later release) so the previous version still runs on the
newer schema.

**Logs.** The workflow runs the script detached from the SSH session, so a dropped connection or a
cancelled job cannot stop it half-way. Its full output is in `deploy/last-deploy.log` on the VM (it
never contains secret values) and is also printed in the Actions log.

**DEPLOY_PATH.** Use an absolute path (for example `/home/ubuntu/home-awareness`). A leading `~/` is
expanded on the VM, but an absolute path is the safe choice.
