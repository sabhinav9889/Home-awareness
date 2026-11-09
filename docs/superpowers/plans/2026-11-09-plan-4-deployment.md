# Plan 4: Deployment and Automation (Oracle Always Free)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Anyone can run the whole system on a free cloud VM over HTTPS, with automatic deploys after CI passes, nightly backups, and a documented restore. The author does only the account steps; everything else is scripted and tested locally.

**Architecture:** The existing Docker Compose stack plus a production override (`docker-compose.prod.yml`) that adds Caddy (automatic HTTPS for a free DuckDNS name), stops publishing database, Redis and API ports to the host, adds a backup service and a DuckDNS updater. Deploys run on the VM (`deploy/deploy.sh`: build, migrate, switch, health-check, roll back on failure) and are triggered by a GitHub Actions workflow over SSH after CI succeeds. The VM is Arm, so the images are built there natively.

**Tech Stack:** Docker Compose, Caddy 2, PostgreSQL 16 (`pg_dump`), Bash (checked with shellcheck), GitHub Actions, DuckDNS, optional rclone to Oracle Object Storage.

**Spec:** `docs/design.md` §10 Hosting (Oracle Always Free main, no-card fallback, HTTPS via DuckDNS + Caddy, nightly backups, portability) and §16 done criteria. Camera photos need a persistent disk, which is why Oracle is the main host.

## Global Constraints
- **$0 and no card surprises:** only Oracle "Always Free" resources; the guide says never to upgrade to Pay As You Go, and to sign in at least every 60 days.
- No secret is ever committed or printed. Secrets live in `deploy/.env.prod` on the VM (mode 0600, git-ignored) and in GitHub Actions secrets. Scripts must not echo them. `gen_secrets` prints nothing but the file path.
- Production never publishes database, Redis or API ports on the host; only Caddy listens on 80 and 443.
- One Docker image for all roles; all configuration through environment variables (portability for the no-card fallback).
- Everything must be testable without a cloud account: local checks use `DOMAIN=localhost` with Caddy `tls internal`. Never touch the running local stack, the live `home` database or the author's `.env`. Use a separate compose project name (`-p ha-deploy-test`) and unused host ports for any local run, and remove everything you start.
- Shell scripts: `set -euo pipefail`, pass `shellcheck`, idempotent where they claim to be.
- Existing behaviour and all existing tests stay green. Commits end with the Co-Authored-By line the implementer's harness gives it. Never push.

## Review Focus
1. **Rate limits behind the proxy:** `app/ratelimit.py` keys by `request.client.host`; behind Caddy every client would look like the proxy and share one limit (or one attacker could lock everyone out). Real client IPs must reach the app and must not be spoofable from outside (Task 1).
2. **Deploy fails halfway:** a failed build, failed migration or unhealthy new version must leave the old version serving, and the script must exit non-zero (Task 3).
3. **Backup restore actually works** into an empty database, and a backup run never stops the stack or fills the disk (retention) (Task 2).
4. **Secrets leaking** into logs, process lists, the repo or Actions output (Tasks 1, 3, 4).
5. **Reboot of the VM:** every service comes back by itself with data intact; certificates persist across container recreation (Tasks 1, 4).
6. **A fresh clone with no secrets set** fails early with a clear message, not a half-started stack (Task 1).

---

### Task 1: Production compose, Caddy, secrets, trusted proxy
**Files:** `docker-compose.prod.yml`, `deploy/Caddyfile`, `deploy/.env.prod.example`, `scripts/gen_secrets.py`, `.gitignore` (`deploy/.env.prod`), `Dockerfile` (only if the proxy settings need it), tests `tests/test_deploy_config.py`.
**Interfaces — Produces:**
- `docker-compose.prod.yml`, used as `docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file deploy/.env.prod -p home-awareness ...`:
  - `caddy` service (image `caddy:2-alpine`, ports 80 and 443 (and 443/udp), volumes `caddy_data` and `caddy_config` so certificates survive recreation, `restart: unless-stopped`, depends on a healthy `api`).
  - `db`, `redis`, `api`: `ports: !reset []` (no host ports), `restart: unless-stopped`, log rotation (`json-file` 10m × 3), the database password taken from `POSTGRES_PASSWORD` (required, no default) and used consistently in `DATABASE_URL` for api, worker, beat and migrate.
  - api gets `FORWARDED_ALLOW_IPS=*` and uvicorn keeps `--proxy-headers` (safe because only Caddy can reach the api); Caddy sets `X-Forwarded-For` from the real client and overwrites any client-supplied value.
- `deploy/Caddyfile`: site `{$DOMAIN}`; `reverse_proxy api:8000` (WebSocket works by default); `request_body max_size 9MB` (the app's 8 MB multipart cap plus headroom); security headers (HSTS, `X-Content-Type-Options`, `Referrer-Policy`) without breaking the dashboard's CSP; `tls internal` only when `{$DOMAIN}` is `localhost`; global ACME email from `{$ACME_EMAIL}`.
- `deploy/.env.prod.example` documents every variable: `DOMAIN`, `ACME_EMAIL`, `PUBLIC_BASE_URL` (`https://{$DOMAIN}`), `POSTGRES_PASSWORD`, `JWT_SECRET`, `SNAPSHOT_KEY`, `EMAIL_BACKEND` and SMTP settings, Telegram settings, `NTFY_BASE_URL`, `DUCKDNS_TOKEN` (Task 4).
- `scripts/gen_secrets.py [--out deploy/.env.prod]`: creates the file from the example with fresh `POSTGRES_PASSWORD`, `JWT_SECRET` (48 bytes urlsafe), `SNAPSHOT_KEY` (Fernet), mode 0600; refuses to overwrite an existing file unless `--force`; never prints a secret value.
- [ ] Tests (no Docker needed): `docker compose ... config` is valid (skip if docker missing); the merged config has no host ports for db, redis, api and publishes only 80/443; every required variable is marked required (`:?`) so a missing secret fails early with its name; `gen_secrets` file mode, uniqueness, valid Fernet key, refusal to overwrite; `.env.prod` is git-ignored; the Caddyfile validates (`caddy validate` via `docker run`, skip if docker missing).
- [ ] Local smoke (not a unit test; record the result in the report): `DOMAIN=localhost`, project `ha-deploy-test`, unused ports; `curl -k https://localhost/health` is 200; a WebSocket upgrade to `/ws` reaches the app (a 4xx from auth is fine, a 502 is not); two requests with different `X-Forwarded-For` headers from outside are treated as the real client address Caddy saw, not the forged one; tear everything down.
- [ ] RED → implement → GREEN (`uv run pytest -q && uv run ruff check .`) → commit `feat(deploy): production compose with Caddy HTTPS, secrets generator, trusted proxy`.

### Task 2: Nightly backups and a tested restore
**Files:** `deploy/backup.sh`, `deploy/restore.sh`, `docker-compose.prod.yml` (a `backup` service), `deploy/README.md` (backup section), tests `tests/test_deploy_scripts.py`.
**Interfaces — Produces:**
- `backup` service (small image with `postgres:16-alpine` client tools, `restart: unless-stopped`) that runs `backup.sh` daily at 03:30 server time: `pg_dump` of the `home` database, gzip, written atomically (`.part` then rename) to the `backups` volume as `home-YYYYmmdd-HHMMSS.sql.gz`; keeps 14 daily and 8 weekly (Sunday) files, deletes the rest; refuses to run if free space is under 1 GB; exit non-zero and log a clear line on failure. Snapshots are excluded on purpose (short-lived, by design).
- Optional off-box copy: if `RCLONE_REMOTE` is set (Oracle Object Storage S3-compatible, free tier), `backup.sh` copies the new file after the local write succeeds; the failure of the copy does not delete the local backup; documented step by step.
- `restore.sh FILE [--into DBNAME]`: restores into a new empty database (default `home_restore`), never into the live `home` unless `--into home --yes-overwrite` is given; verifies the table count afterwards.
- [ ] Tests (shell with a stub `pg_dump`/`psql` on PATH, `shellcheck` if available): retention keeps exactly 14 daily + 8 weekly from a generated set of dated files; a failed dump leaves no `.part` and no truncated backup, and exits non-zero; low disk refuses; `restore.sh` refuses the live DB without the flag.
- [ ] Local drill (record in the report): create a throwaway Postgres (project `ha-deploy-test`), load the migrated schema plus a few rows, run `backup.sh`, restore into a scratch database with `restore.sh`, compare row counts, tear everything down.
- [ ] RED → implement → GREEN → commit `feat(deploy): nightly pg_dump backups with retention and a tested restore`.

### Task 3: Deploy script and GitHub Actions workflow
**Files:** `deploy/deploy.sh`, `.github/workflows/deploy.yml`, tests `tests/test_deploy_scripts.py` (extend).
**Interfaces — Produces:**
- `deploy/deploy.sh [GIT_REF]`, run on the VM inside the repo: takes a lock (`flock`, one deploy at a time); records the current commit in `deploy/.last_good` only after a healthy deploy; `git fetch` and checks out `GIT_REF` (default `origin/main`) with a fast-forward-only rule; `docker compose build` (fail → nothing changes); runs `migrate` (fail → stop, old version keeps running); `up -d` with `--wait`; checks `https://$DOMAIN/health` (retries up to 90 s); on any failure after the switch, checks out `.last_good`, rebuilds and restarts it, prints a clear "ROLLED BACK to <sha>" and exits non-zero. Never prints environment values. Prunes unused images at the end (keeps disk free).
  - Note for the plan: Alembic migrations in this project are forward-only in production; a rollback after a migration that changed the schema is not automatic. The script must say so when the rolled-back commit has fewer migrations than the database.
- `.github/workflows/deploy.yml`: triggers on `workflow_run` of workflow `ci` completing on `main` with conclusion `success`, plus manual `workflow_dispatch`; `concurrency: deploy` with no cancellation mid-run; does nothing (a green skip with a notice) when the secret `DEPLOY_HOST` is not set; otherwise SSH with the key in `DEPLOY_SSH_KEY` and a pinned `DEPLOY_HOST_KEY` known_hosts entry (no `StrictHostKeyChecking=no`), runs `cd $DEPLOY_PATH && ./deploy/deploy.sh <sha>` where `<sha>` is the CI-verified commit; the key is written to a 0600 file and removed at the end; secrets are never echoed; permissions `contents: read`.
- [ ] Tests (stub `docker`, `git`, `curl`, `flock` on PATH): healthy deploy updates `.last_good`; build failure leaves everything untouched and exits non-zero; unhealthy new version triggers a rollback to `.last_good` and exits non-zero; two concurrent runs: the second waits or refuses; no secret value appears in output; workflow YAML parses, has the secret guard, pinned host key, minimal permissions, and uses the CI-verified sha (a test that reads the YAML).
- [ ] RED → implement → GREEN → commit `feat(deploy): safe deploy script with rollback and GitHub Actions auto-deploy`.

### Task 4: VM bootstrap, DuckDNS, monitoring hooks
**Files:** `deploy/bootstrap-vm.sh`, `docker-compose.prod.yml` (a `duckdns` service), `deploy/README.md`, tests `tests/test_deploy_scripts.py` (extend).
**Interfaces — Produces:**
- `deploy/bootstrap-vm.sh` (Ubuntu on Oracle Arm, run once as the default user with sudo, idempotent): installs Docker Engine and the compose plugin from Docker's repository, adds the user to the docker group, opens ports 80 and 443 in the host firewall in the way Oracle Ubuntu images need (their default iptables rules reject them), enables `unattended-upgrades`, adds 2 GB swap if absent, creates `~/home-awareness` clone path hints, and prints the remaining manual steps; supports `--dry-run` that prints what it would do and changes nothing.
- `duckdns` service (a tiny image or `curlimages/curl` loop, every 5 minutes) updating `${DOMAIN}`'s record with `DUCKDNS_TOKEN`; the token never appears in logs; a failure is logged once per state change.
- A `/health` check endpoint reachable from outside is the hook for the free UptimeRobot monitor (documented in Task 5); confirm `GET /health` needs no auth and reveals nothing sensitive.
- [ ] Tests: `bootstrap-vm.sh --dry-run` prints the steps and runs no `apt`, `docker`, `iptables`, `sudo` or `systemctl` commands (stubs on PATH assert that); shellcheck passes; the duckdns loop script never prints the token (stubbed curl).
- [ ] RED → implement → GREEN → commit `feat(deploy): VM bootstrap script and DuckDNS updater`.

### Task 5: The author's step-by-step guide and the production checks
**Files:** `docs/deploy-oracle.md`, `README.md` (pointer), `docs/design.md` (§10 status note), `deploy/README.md`.
- `docs/deploy-oracle.md`, written for someone who has never used Oracle Cloud, in order, with the exact clicks and commands and what to expect after each step:
  1. Create the Oracle account (the card is for identity verification only; never click Upgrade; Always Free labels; sign in every 60 days; what to do if Arm capacity is unavailable: retry later, or use the no-card fallback).
  2. Create the Always Free Arm VM (Ubuntu), add the SSH key, open ports 80 and 443 in the VCN security list (and SSH only from the author's IP if practical), note the public IP and reserve it.
  3. Create the DuckDNS name and token and point it at the IP.
  4. SSH in, run `bootstrap-vm.sh`, clone the repo, run `scripts/gen_secrets.py`, edit `deploy/.env.prod` (domain, email, SMTP), first `docker compose ... up -d --build`.
  5. Sign up in the browser at `https://NAME.duckdns.org/app/`; email: explain that without SMTP the confirmation link appears in the API log and how to get it, and the free SMTP options (a transactional-mail free tier such as Brevo or Resend); **never ask the author to put a Gmail password anywhere**.
  6. Point sensors at the cloud server (`ha-sensor pair --server https://...`, `ha-camera pair --server ...`).
  7. GitHub secrets for auto-deploy (`DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_HOST_KEY`, `DEPLOY_PATH`), a dedicated deploy key, and how to run the first automatic deploy.
  8. Monitoring (UptimeRobot free HTTP check on `/health` with an email alert) and the backup check (`docker compose ... logs backup`, a restore drill with `restore.sh`).
  9. Updating, rolling back, and what each failure message means.
  10. The no-card fallback in two paragraphs, with the honest limits (sleeps when idle, no worker, temporary disk, so camera photos are lost on restart).
- A "production checklist" section that lists what was verified locally and what can only be verified on the real VM (Arm image builds, Oracle firewall, certificate issuance, DuckDNS), so nothing is claimed that was not tested.
- [ ] Docs only; `uv run ruff check .`; commit `docs: Oracle Always Free deployment guide`.

---
## Self-review notes
Design §10 → T1 (HTTPS, portability), T2 (backups), T3 (automation), T4 (DuckDNS, bootstrap), T5 (guide, fallback); §16 done criteria "docker compose up brings up ... Caddy" → T1; "live on Oracle" and "deployed once to the fallback" need the author's accounts and are explicitly left for the guide. The in-process job runner for the fallback already exists (`JOB_RUNNER`). Known gaps stated up front: Arm builds and certificate issuance can only be proven on the VM; production schema rollbacks are not automatic.
