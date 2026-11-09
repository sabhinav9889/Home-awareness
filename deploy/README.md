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
- Backup files are owner-only (mode 0600 in a 0700 directory). `pg_dump` is cut off after
  30 minutes (`DUMP_TIMEOUT_S`), and `docker stop` ends a running backup within a second or two
  and removes the partial file.
- Check `docker compose ... logs --since 48h backup` once a week: a failing backup only logs.
- After a deploy that changes `deploy/backup.sh` or `deploy/restore.sh`, run
  `docker compose ... up -d --force-recreate backup`: the scripts are single-file bind mounts,
  which keep pointing at the old file when git replaces it.
- Keep the backup image's Postgres major version in sync with the `db` image (both 16); a dump
  from a newer `pg_dump` than the server is refused, and an older client may not read a newer one.

Run one by hand and list the results:

    docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file deploy/.env.prod \
      -p home-awareness exec -T backup bash /deploy/backup.sh
    docker compose ... exec -T backup ls -l /backups

### Restore (practise this once, into a scratch database)

    docker compose ... exec -T backup bash /deploy/restore.sh /backups/home-20261109-033000.sql.gz

This creates `home_restore`, loads the file, and prints the table count and a few row counts.
The file is checked first (`gzip -t`, contains `CREATE TABLE`, and ends with pg_dump's
`-- PostgreSQL database dump complete` line); a truncated file is refused before anything changes.
A target that already has tables (in any non-system schema) is refused unless `--yes-overwrite`,
and the live `home` database is refused unless you pass both `--into home --yes-overwrite` (stop
`api`, `worker` and `beat` first; a restore over `home` replaces everything in it). The old
tables are dropped and the dump is loaded in ONE transaction, so a dump that fails halfway rolls
back and the database keeps its previous contents. `postgres`, `template0` and `template1` are
never accepted as targets. To copy a backup off the VM: `docker compose ... cp backup:/backups/FILE .`.

To load it into another Postgres (for example the Neon fallback):

    gzip -dc FILE | psql -v ON_ERROR_STOP=1 -1 "$NEON_DATABASE_URL"

Use Neon's direct (non-pooled) connection string and an EMPTY target database. The dump only
needs the extensions Neon supports (this schema uses none beyond the defaults; check
`CREATE EXTENSION` lines in the file if you add some). Without `ON_ERROR_STOP` a partial restore
prints errors but still looks like success; with `-1` it is all or nothing.

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

## VM bootstrap

`deploy/bootstrap-vm.sh` prepares a fresh Ubuntu 22.04/24.04 VM once (written for Oracle Cloud Always
Free Ampere A1, aarch64; the 1 GB x86 micro shapes are too small for this stack). Run it as the
normal user (`ubuntu`), never as root; it calls `sudo` per step and refuses to run as root.

```
deploy/bootstrap-vm.sh --dry-run   # prints every step as "would: ...", changes nothing
deploy/bootstrap-vm.sh
```

It installs Docker Engine and the compose plugin from Docker's apt repository (the signing key's
fingerprint is checked against the published one), adds you to the `docker` group, opens 80/tcp,
443/tcp and 443/udp in the host firewall, enables `unattended-upgrades` and creates a 2 GB swap file
if there is no swap. A second run changes nothing and says so.

**Oracle firewall.** Oracle's Ubuntu images load iptables rules that accept SSH and then REJECT
everything else. The script inserts its rules before that REJECT rule (never touching the SSH rule)
and writes them into `/etc/iptables/rules.v4` so they survive a reboot. That file is rewritten
through a temp file checked with `iptables-restore --test`, keeping its mode and owner, and the
first original is kept as `rules.v4.ha-bak` (never overwritten). If the file is missing it is created
from the INPUT chain only; Docker's chains are never persisted. If the INPUT chain has no safe
insertion point the script stops with an error instead of pretending. It never enables a firewall.
**If ufw is installed the firewall step is skipped** (installing `iptables-persistent` would remove
ufw) and the script prints `sudo ufw allow 80/tcp`, `443/tcp` and `443/udp` for you to run yourself.
The firewall step is IPv4 only: if the VM ever gets IPv6, add the same three ACCEPT rules with
`ip6tables` (and persist them in `/etc/iptables/rules.v6`) or the ports stay closed over IPv6. The cloud firewall is separate: also add
ingress rules for TCP 80, TCP 443 and UDP 443 to the VCN security list or network security group.

After it finishes it prints the remaining manual steps: log out and in (docker group), clone the
repository, `python3 scripts/gen_secrets.py`, edit `deploy/.env.prod`, run `deploy/deploy.sh`.

## DuckDNS (optional)

For a free `yourname.duckdns.org` address whose IP may change, the optional `duckdns` service calls
DuckDNS every 5 minutes. It is off unless its compose profile is enabled, so users with their own
domain or a reserved IP do not run it.

1. Create the subdomain and copy your token from duckdns.org; set `DOMAIN=yourname.duckdns.org` and
   `DUCKDNS_TOKEN=...` in `deploy/.env.prod`. (`DUCKDNS_SUBDOMAIN` overrides the name derived from
   `DOMAIN`; without it, a `DOMAIN` that does not end in `.duckdns.org` makes the service exit with a
   clear message.)
2. Enable the profile by adding the line `COMPOSE_PROFILES=duckdns` to `deploy/.env.prod`.
   `deploy/deploy.sh` passes that file with `--env-file`, and compose reads `COMPOSE_PROFILES` from
   it, so every deploy (including the GitHub Actions one, whose SSH session has no such variable)
   starts and updates the service. By hand you can use `--profile duckdns` instead.

The token is handed to curl on stdin only, so it is not in the process list, and neither the URL
nor the response is logged. The log has one line per state change (`OK`, then `KO` or `OK again`),
not one per check. Look at it with `docker compose ... logs duckdns`.

## Monitoring

`GET /health` needs no authentication and returns only `{"status":"ok","db":"ok","redis":"ok"}`
(HTTP 200 when the database and Redis answer, an error status otherwise). To monitor it for free,
create an UptimeRobot account, add an HTTP(s) monitor for `https://DOMAIN/health` with a 5 minute
interval and alert contacts of your choice. Pair it with the Oracle rule of signing in at least
every 60 days so the always-free account stays active.

## Operating notes

- The `docker` group is root-equivalent: anyone in it can take over the VM. Keep it to your own user.
- `unattended-upgrades` installs security updates but never reboots by design. After a kernel update
  (`/var/run/reboot-required` exists) reboot once in a while: `sudo reboot`, then check the stack came
  back (`restart: unless-stopped` starts it).
- `deploy/duckdns.sh` is bind-mounted as a single file, so a deploy that changes it may leave the
  running container on the old inode. After such a deploy run
  `docker compose ... --profile duckdns up -d --force-recreate duckdns`.
- A misconfigured DuckDNS service (bad `DOMAIN`, missing token) does not fail deploys: it logs the
  reason and idles, repeating it hourly. Fix `deploy/.env.prod`, then recreate the service as above.
  Its `curl -4` always uses IPv4, so DuckDNS never records an IPv6 address.
- **/metrics is hidden.** Caddy answers 404 for `/metrics` (and anything below it), because the
  endpoint has no authentication and shows activity counters. Read it from inside the stack:
  `docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file deploy/.env.prod
  -p home-awareness exec api python -c "import urllib.request as u;print(u.urlopen('http://localhost:8000/metrics').read().decode())"`.
