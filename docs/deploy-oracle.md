# Deploy Home Awareness on a free Oracle Cloud VM

A step-by-step guide for putting the whole system on an Oracle Cloud "Always Free" VM, written
for someone who has never used Oracle Cloud and is not a Linux expert. Reference material that
this guide links to instead of repeating: [deploy/README.md](../deploy/README.md).

Oracle's console changes its screens and names over time. This guide describes what to look for,
not pixel-exact clicks. If a name differs, search the console's search bar for the word in
**bold**.

Replace `NAME` with your DuckDNS name, `IP` with your VM's public IP, and `CODE` with a pairing code.

## 1. What you are about to do

You will rent one small virtual machine (VM) from Oracle for $0, install Docker on it, and run
the whole stack there so alerts keep working when your laptop is off.

| Piece | What it is |
| --- | --- |
| VM | An Always Free Ampere A1 (Arm) Ubuntu machine in Oracle Cloud. |
| Caddy | A web server in a container. It gets a free Let's Encrypt HTTPS certificate by itself and is the only thing exposed (ports 80 and 443). |
| DuckDNS | A free name such as `NAME.duckdns.org` pointing at the VM's IP. |
| Backups | A `backup` container that dumps the database every night at 03:30 UTC and keeps the newest ones on the VM. |
| Auto-deploy | A GitHub Actions workflow that, after CI passes on `main`, logs in to the VM over SSH and runs `deploy/deploy.sh`, with rollback. Optional. |
| Monitoring | A free UptimeRobot check on `https://NAME.duckdns.org/health` that emails you when it fails. |

Time: about 2 hours for the first time, plus waiting if Oracle has no free Arm capacity.

Cost: $0. Oracle's documentation says an account that was never upgraded is not charged.
Things that could cost money if you click the wrong one: the **Upgrade** or **Pay As You Go**
buttons, any shape or storage not labelled **Always Free**, and going over the Always Free
totals. Never click those.

## 2. Create the Oracle account

1. Go to <https://www.oracle.com/cloud/free/> and choose to start for free.
2. Fill in your details and choose a **home region** near you. It cannot be changed later, and
   Always Free resources must be created in it.
3. Add a card. It is for identity verification only. You may see a small temporary hold.
4. Wait for the "account ready" email, then sign in to the console.

Rules to keep it free:

- **Never click Upgrade or Pay As You Go**, even if the console nags you.
- Create only resources labelled **Always Free** (or "Always Free eligible").
- The account starts with a time-limited trial that has credits; stay inside the Always Free
  limits so nothing depends on trial credits.
- **Sign in at least every 60 days.** Oracle can reclaim idle Always Free resources.

If it doesn't work:

- Sign-up refused (card or phone verification fails): try another card or a different browser
  without extensions, and contact Oracle support. If it still fails, use the no-card fallback
  in [section 13](#13-the-no-card-fallback-render--neon--upstash).
- "Out of capacity" for the Arm shape later: see the note in section 3.

## 3. Create the Always Free Arm VM

Commonly documented Always Free limits: up to 4 Arm OCPUs and 24 GB of memory in total, and
200 GB of block storage. Oracle has changed these before (the design notes mention 2 OCPU /
12 GB), so **trust the "Always Free" label in the console, not this paragraph**. This stack
needs the Arm shape; `deploy/bootstrap-vm.sh` states that the 1 GB x86 micro shapes are too small.

**3a. SSH key.** On your laptop, if `~/.ssh/id_ed25519.pub` does not exist:

```bash
ssh-keygen -t ed25519 -C "oracle-vm"
cat ~/.ssh/id_ed25519.pub
```

You should see: one line starting with `ssh-ed25519`. That is the public key; never share the
file without `.pub`.

**3b. Create the instance.** In the console open the menu, **Compute** then **Instances**,
then **Create instance**.

- Name: anything, for example `home-awareness`.
- Image: **Canonical Ubuntu** 22.04 or 24.04.
- Shape: **Change shape**, choose **Ampere** and `VM.Standard.A1.Flex`. The shape must carry
  the **Always Free** label. Start with 2 OCPUs and 12 GB (or 4 and 24 if your allowance shows it).
- Networking: create a new VCN with a **public subnet** and **assign a public IPv4 address**.
- SSH keys: paste the contents of your `id_ed25519.pub`.
- Boot volume: the default (about 50 GB) is fine and within the free 200 GB.
- Create. Wait until the state is **Running**.

If you see "Out of capacity for shape VM.Standard.A1.Flex": this is common. Retry later
(early morning often works), try another availability domain in your home region if it
offers one, or try fewer OCPUs. You cannot switch region without a new home-region account.
If nothing works for days, use the no-card fallback (section 13).

**3c. Open the ports in the cloud firewall.** Oracle blocks inbound traffic by default. On the
instance page click the subnet, then its **Security List** (or the instance's **Network
Security Group**), then **Add Ingress Rules**:

| Source CIDR | Protocol | Destination port | Why |
| --- | --- | --- | --- |
| `0.0.0.0/0` | TCP | `80` | Let's Encrypt validation and redirect to HTTPS |
| `0.0.0.0/0` | TCP | `443` | HTTPS |
| `0.0.0.0/0` | UDP | `443` | HTTP/3 |

Port 22 (SSH) usually already has a rule from `0.0.0.0/0`. If your home IP rarely changes,
edit it to your own address with `/32` (find it by searching "what is my IP"). If your IP
changes, leave SSH open: password login is off on Oracle's Ubuntu images and only your key works.
Auto-deploy (section 10) connects from GitHub's changing addresses, so if you use it, keep
SSH open to `0.0.0.0/0`.

**3d. Reserve the public IP.** Without this, stopping and starting the VM can change its IP.
Open the instance, **Attached VNICs**, the primary VNIC, **IPv4 Addresses**, edit the address,
choose **Reserved public IP** (create a new one). Check that it is labelled **Always Free**
(Oracle has documented reserved IPs as free while attached to a running instance; confirm
the label).

You should see: the instance page shows your public IP. Write it down as `IP`.

**3e. Connect.**

```bash
ssh ubuntu@IP
```

You should see: a prompt like `ubuntu@home-awareness:~$` (type `yes` once for the host key).

If it doesn't work: "Connection timed out" means the security list rule for port 22 is
missing or the IP is wrong. "Permission denied (publickey)" means the wrong key; use
`ssh -i ~/.ssh/id_ed25519 ubuntu@IP`.

## 4. DuckDNS: a free name

1. Go to <https://www.duckdns.org> and sign in with an existing account (GitHub, Google, etc.).
   No card is needed.
2. Type a name in the **domain** box and press **add domain**. Your address is
   `NAME.duckdns.org`.
3. In the row for the name, put `IP` in the **current ip** box and press **update ip**.
4. Copy your **token** from the top of the page. It is a secret: treat it like a password.

Test from your laptop (DNS can take a minute):

```bash
nslookup NAME.duckdns.org
# or: dig +short NAME.duckdns.org
```

You should see: your `IP`.

If the VM's IP ever changes (it will not with a reserved IP), the name would point at nothing
and HTTPS would fail. The optional `duckdns` container prevents that by calling DuckDNS every
5 minutes. You enable it in step 6 by adding `COMPOSE_PROFILES=duckdns` to `deploy/.env.prod`
together with `DUCKDNS_TOKEN`. With a reserved IP it is optional.

## 5. Bootstrap the VM

On the VM (`ssh ubuntu@IP`). Run as `ubuntu`, never as root.

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/sabhinav9889/Home-awareness.git ~/home-awareness
cd ~/home-awareness
deploy/bootstrap-vm.sh --dry-run
```

You should see: lines starting `would: ...` for each step (install Docker, add you to the
`docker` group, open the firewall, unattended upgrades, swap) and "dry run finished; nothing
was changed".

Then for real:

```bash
deploy/bootstrap-vm.sh
```

You should see: `bootstrap: finished` and a list of "Remaining manual steps". It is safe to run
again; a second run says `nothing to change, this machine is already bootstrapped`.

Log out and back in so the `docker` group applies, then check:

```bash
exit
ssh ubuntu@IP
docker compose version
```

You should see: `Docker Compose version v2.24` or newer (the deploy script refuses older).

The `docker` group is root-equivalent: anyone who can run `docker` can take over the VM. Keep
it to your own user.

If it doesn't work: if `ufw` is installed the script skips the host firewall and prints the
three `sudo ufw allow` commands for you to run. If it stops with a firewall error, the
message says what it found; send it to the assistant (section 14).

## 6. Secrets and configuration

`scripts/gen_secrets.py` writes `deploy/.env.prod` with fresh random secrets (mode 0600) and
prints only the file path. It imports `cryptography` (for the Fernet snapshot key), which a fresh
Ubuntu does not have, so plain `python3 scripts/gen_secrets.py` (the command the bootstrap
script prints) fails with `ModuleNotFoundError: No module named 'cryptography'`. Install the
Ubuntu package first; the rest of the script uses only the standard library:

```bash
sudo apt-get install -y python3-cryptography
cd ~/home-awareness
python3 scripts/gen_secrets.py
```

You should see: `deploy/.env.prod` (its full path). It refuses to overwrite an existing file
unless you pass `--force` (keeps existing secrets) or `--rotate` (replaces all; read its
`--help` first, rotating breaks the database password, stored snapshots and logins).

**Back up the secrets now.** Show the file in your terminal, copy `POSTGRES_PASSWORD`,
`JWT_SECRET` and `SNAPSHOT_KEY` into a password manager, then clear the screen:

```bash
cat deploy/.env.prod
clear
```

Losing `SNAPSHOT_KEY` makes stored camera photos unreadable. The database backups do not
contain these values.

Now edit the file:

```bash
nano deploy/.env.prod
```

(Save with Ctrl+O, Enter; exit with Ctrl+X.) Set:

| Variable | Value |
| --- | --- |
| `DOMAIN` | `NAME.duckdns.org` |
| `ACME_EMAIL` | your email, for Let's Encrypt expiry notices |
| `ACME_CA` | for the first tries, remove the leading `#` from the staging line `ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory` |
| `DUCKDNS_TOKEN` and `COMPOSE_PROFILES=duckdns` | only if you want the DuckDNS updater (step 4) |

Staging certificates are not trusted by browsers, which is the point: Let's Encrypt's
production service limits failed attempts, and staging does not. You remove the staging
line in step 7.

You should see: `grep -c '=.' deploy/.env.prod` prints a number, and
`docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file deploy/.env.prod -p home-awareness config -q`
prints nothing. (Never run `config` without `-q`; it prints secrets.) A missing required
variable stops it with a message naming the variable.

## 7. First start

The full command is long; set a shortcut for this terminal session:

```bash
dc() { docker compose -f docker-compose.yml -f docker-compose.prod.yml --env-file deploy/.env.prod -p home-awareness "$@"; }
```

(You must retype this after logging in again.) Start with staging still enabled:

```bash
dc up -d --build
```

The first build on Arm takes several minutes. Watch it:

```bash
dc ps
dc logs -f caddy
```

You should see: `dc ps` lists `db`, `redis`, `api`, `worker`, `beat`, `caddy`, `backup`
(and `duckdns` if enabled) as running or healthy; `migrate` has exited with code 0. In the
Caddy log, a line mentioning the certificate being obtained from `acme-staging-v02`. Stop
following with Ctrl+C.

Open `https://NAME.duckdns.org/app/` in a browser.

You should see: a browser warning ("Your connection is not private", `NET::ERR_CERT_AUTHORITY_INVALID`
or similar). That is the staging certificate working: choose Advanced and proceed once to see
the app's sign-in page. Also `curl -k https://NAME.duckdns.org/health` should print
`{"status":"ok","db":"ok","redis":"ok"}`.

If it doesn't work: Caddy log lines with `challenge failed` or `timeout` mean Let's Encrypt
cannot reach you: check DNS (`nslookup`), the security list rules (section 3c) and the host
firewall (`sudo iptables -L INPUT -n | head -20`).

**Switch to the real certificate.** Edit `deploy/.env.prod` and put the `#` back in front of
the `ACME_CA=` line (or delete it), then:

```bash
dc up -d caddy
dc logs --since 5m caddy
```

You should see: a certificate obtained from Let's Encrypt production; no browser warning on
`https://NAME.duckdns.org/app/`.

Do not run `deploy/deploy.sh` while staging is active: its health check verifies the certificate
and would treat the untrusted staging one as a failure. (If you need to, `CURL_EXTRA=-k`
is the documented override.)

## 8. Email

By default `EMAIL_BACKEND=console`: the app writes sign-up confirmation and password-reset
emails to the api's log instead of sending them. Read the link with:

```bash
dc logs api | grep token=
```

Copy the whole link into your browser. (The console backend writes straight to stderr so the
token is not masked; never use it where logs are shared.)

For real emails use a transactional email service with a free tier, such as Brevo or Resend.
Create an account, verify your sender address or domain as the service asks, and take its SMTP
host, port, user and password. Then set in `deploy/.env.prod`:

```
EMAIL_BACKEND=smtp
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...
SMTP_FROM=Home Awareness <you@yourdomain.example>
```

and apply: `dc up -d`. The app requires `SMTP_HOST` and `SMTP_FROM` when the backend is `smtp`,
and logs in with STARTTLS when a user is set. Free-tier terms change often; check the service's
current limits. **Never put your personal Gmail password anywhere.** You can skip email at the
start: the console backend is enough for one user.

## 9. Sign up, add rooms and sensors, point your devices at the cloud

1. Open `https://NAME.duckdns.org/app/`, **Sign up**, then confirm with the link from step 8.
2. In the dashboard open **Sensors** to create a pairing code (valid 10 minutes, usable
   once). For a camera use **Add a camera** (see [docs/camera-live-test.md](camera-live-test.md)).
3. On each sensor laptop, from the repository's `edge/` directory (see
   [edge/README.md](../edge/README.md)):

```bash
uv run ha-sensor pair --server https://NAME.duckdns.org --code CODE
uv run ha-sensor run
uv run ha-camera pair --server https://NAME.duckdns.org --code CODE
uv run ha-camera run
```

You should see: the sensor card on the dashboard turns online within a minute or so. Once
sensors are paired to the cloud, the laptop no longer needs to run the server; it only
runs `ha-sensor`, `ha-camera` or `ha-agent`.

If it doesn't work: HTTP 404 means a wrong `--server`; rejected events with a clock complaint
mean the laptop clock is more than 10 minutes off. Sensors paired to a local server must be
paired again.

## 10. Auto-deploy from GitHub (optional)

After CI passes on `main`, `.github/workflows/deploy.yml` logs in to the VM and runs
`deploy/deploy.sh <sha>`. Until the secrets exist it does nothing and finishes green with a
notice.

**10a. A dedicated deploy key.** On your laptop (not the VM), make a key used for nothing else:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/ha_deploy -N "" -C "github-deploy"
ssh-copy-id -i ~/.ssh/ha_deploy.pub ubuntu@IP
```

If `ssh-copy-id` is not available, append the contents of `ha_deploy.pub` to
`~/.ssh/authorized_keys` on the VM.

**10b. The pinned host key line.** The workflow refuses unknown hosts. Get the VM's key:

```bash
ssh-keyscan -t ed25519 IP
```

You should see: one line `IP ssh-ed25519 AAAA...`. Verify it is really your VM: on the VM
(not over this scan) run `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`, and on your
laptop run `ssh-keyscan -t ed25519 IP | ssh-keygen -lf -`; the fingerprints must match.

**10c. The five secrets.** In your GitHub repository: Settings, Secrets and variables,
Actions, New repository secret:

| Secret | Value |
| --- | --- |
| `DEPLOY_HOST` | the VM's IP or hostname (only letters, digits, `.`, `_`, `-`) |
| `DEPLOY_USER` | `ubuntu` |
| `DEPLOY_SSH_KEY` | the whole contents of `~/.ssh/ha_deploy` (the private key, including the BEGIN and END lines) |
| `DEPLOY_HOST_KEY` | the `ssh-keyscan` line from 10b, exactly |
| `DEPLOY_PATH` | `/home/ubuntu/home-awareness` (an absolute path; a leading `~/` also works but absolute is safer) |

**10d. Make the VM a clean deploy target.** `deploy.sh` refuses a checkout with local changes to
tracked files, and it needs a fast-forward from the checked-out commit. Do not edit tracked
files on the VM.

**10e. The first deploy.** If you started the stack by hand (step 7), there is no
`deploy/.last_good` file yet. `deploy.sh` handles that: if the running stack is healthy right
before the switch, it uses the current checkout as the rollback target, and prints
`the running stack is healthy; <sha> is the rollback target`. So run the first one by hand on
the VM, with staging switched off, so you see it work:

```bash
cd ~/home-awareness
deploy/deploy.sh
```

You should see: `deploy: <old sha> -> <new sha> ...` and finally `deploy: OK, now running <sha>`.
If the stack was not healthy before it started, there is no rollback target and a failed
switch prints `FIRST DEPLOY FAILED` and stops api, worker and beat (db, redis and caddy keep
running); fix the cause and run it again.

Then in GitHub: Actions, **deploy**, **Run workflow** on `main`. From then on it runs after every
green CI run on `main`.

You should see: in the Actions log the deploy output ending with `deploy: OK, now running <sha>`.
A failed deploy that rolled back shows `ROLLED BACK to <sha>` followed by
`deploy: the deploy of <sha> FAILED (the old version is serving again)`, and the job is red.
`ROLLBACK FAILED, manual action needed` means both versions are unhealthy (section 12).

On the VM the same output is in `deploy/last-deploy.log`:

```bash
tail -n 50 ~/home-awareness/deploy/last-deploy.log
```

Rules to remember:

- **Migrations must be additive.** A rollback never runs `migrate` and never downgrades the
  database, so the old code must run on the newer schema. Add columns and tables first; drop or
  rename only in a later release.
- Automatic deploys only run for commits CI passed on `main` (push events from this repository).
  A manual **Run workflow** deploys the tip of `main` whether or not its CI is green, and only
  people with write access can start it.

## 11. Monitoring and backups

**UptimeRobot.** Create a free account at uptimerobot.com, **Add New Monitor**, type **HTTP(s)**,
URL `https://NAME.duckdns.org/health`, a 5 minute interval, and your email as the alert
contact. Free-plan terms change; check them.

You should see: the monitor turns **Up**. `/health` needs no login and returns only
`{"status":"ok","db":"ok","redis":"ok"}`.

**Backups.** The first one is made when the backup container starts if it is past 03:30 UTC
and none exists yet today; otherwise at 03:30 UTC. Check:

```bash
dc logs --since 48h backup
dc exec -T backup ls -l /backups
```

You should see: log lines for a finished backup and files named `home-YYYYmmdd-HHMMSS.sql.gz`.
A failing backup only logs, so check once a week. To force one now:

```bash
dc exec -T backup bash /deploy/backup.sh
```

**Restore drill (do this once).** It loads a backup into a scratch database `home_restore`,
never into the live one:

```bash
dc exec -T backup bash /deploy/restore.sh /backups/home-YYYYmmdd-HHMMSS.sql.gz
```

(Use a real file name from the `ls` above.) You should see: `restore: database home_restore restored`,
a `tables:` count and some row counts. Running it again is refused because the target already
has tables; that is intended. Restoring over the live `home` database needs
`--into home --yes-overwrite` and stopping `api`, `worker` and `beat` first; see
[deploy/README.md](../deploy/README.md#restore-practise-this-once-into-a-scratch-database).

Backups live on the same VM. An optional off-box copy to Oracle Object Storage is in
[deploy/README.md](../deploy/README.md#optional-off-box-copy-oracle-object-storage-s3-compatible-always-free).
Camera snapshots are deliberately not backed up. Keep `SNAPSHOT_KEY`, `JWT_SECRET` and
`POSTGRES_PASSWORD` in your password manager.

## 12. Day-two operations

Use `dc` from section 7 (retype the function after each login).

| Task | Command |
| --- | --- |
| Update to the newest `main` | `cd ~/home-awareness && deploy/deploy.sh` |
| Deploy one commit | `deploy/deploy.sh <40-hex sha>` |
| Roll back by hand | `git checkout --detach $(cat deploy/.last_good)`, then `dc build api worker beat`, then `dc up -d --no-deps --wait api worker beat` (the same steps the script's rollback uses; the database stays as it is) |
| Logs | `dc logs --since 1h api` (also `worker`, `beat`, `caddy`, `backup`, `duckdns`) |
| Restart one service | `dc restart api` |
| Status | `dc ps` |
| Disk usage | `df -h /` and `docker system df` |
| Reboot after kernel updates | `[ -f /var/run/reboot-required ] && sudo reboot` (the stack restarts itself) |

Certificate renewal is automatic: Caddy renews well before expiry, and the certificate data
lives in the `caddy_data` volume. Keep ports 80 and 443 open.

After a deploy that changes `deploy/backup.sh`, `deploy/restore.sh` or `deploy/duckdns.sh`,
recreate that service (`dc up -d --force-recreate backup`, or `duckdns`) because single-file
bind mounts keep the old file.

What `deploy.sh` messages mean:

| Message | Meaning and action |
| --- | --- |
| `another deploy is already running` | A deploy holds the lock. Wait, then retry. |
| `the checkout has local modifications to tracked files` | Someone edited files on the VM. `git status`, then discard or commit. |
| `is not a fast-forward of the checked-out ...` | The VM checkout is not an ancestor of the target. Look at `git log`; do not force. |
| `Docker Compose 2.24 or newer is required` | Update Docker. |
| `.env.prod is missing or unreadable` / `DOMAIN is not set to a valid hostname` | Fix `deploy/.env.prod`. |
| `git fetch from origin failed` | Network or GitHub problem; nothing was changed. |
| `FAILED at build` / `FAILED at migrate` | The old version keeps running. Read the output above it. |
| `new version ... failed its start or health check` | Followed by a rollback. |
| `ROLLED BACK to <sha>` | The old version serves again; the deploy still counts as failed. |
| `ROLLBACK FAILED, manual action needed` | Both versions unhealthy; run `dc ps` and `dc logs --since 15m api`, then fix or run the manual rollback. |
| `FIRST DEPLOY FAILED` | No earlier good version; api, worker and beat were stopped. Fix and re-run. |
| `WARNING: migrations in ... are not in ...` | The failed commit added migrations; the database was not downgraded. |

**If Oracle stops or reclaims the VM.** Sign in to the console and start the instance (the
reserved IP stays). The stack restarts itself (`restart: unless-stopped`). If the instance
is gone, create a new VM (section 3), re-run sections 5 to 7, copy `deploy/.env.prod` values from
your password manager, and restore the newest backup if you copied one off the VM
(otherwise the data is lost). This is why the off-box copy exists. Also **sign in to Oracle at
least every 60 days.**

## 13. The no-card fallback (Render + Neon + Upstash)

If Oracle sign-up or Arm capacity keeps failing, the design's fallback needs no card: a Render
free web service (the app), Neon free Postgres and Upstash free Redis. **It has not been
deployed or tested in this project, and the design says it should be tested once; that is
still open.** There are no steps here because none could be verified.

What the code supports: all configuration is environment variables only (see `.env.example` and
`app/config.py`), and `JOB_RUNNER=inprocess` runs alert delivery inside the API process for hosts
without a worker. A backup can be loaded into Neon with the command in
[deploy/README.md](../deploy/README.md#restore-practise-this-once-into-a-scratch-database).

Limits, from the design notes: Render's free service sleeps after 15 minutes without traffic
(sensor heartbeats every minute may keep it awake; untested), has no free background worker, and
its disk is temporary, so camera photos are lost on every restart. Everything else here
(`deploy.sh`, Caddy, DuckDNS, backups) is specific to the VM and does not apply.

## 14. What was verified and what was not

| Verified locally in this project (without a real VM) | Not verified: needs the real VM or services |
| --- | --- |
| The compose files render; only 80/tcp, 443/tcp and 443/udp are published | Docker image builds on Arm (Oracle's Ampere A1) |
| TLS with Caddy's internal CA on `localhost` | Oracle's security list and the VM's real firewall |
| A WebSocket works through the proxy | Real Let's Encrypt issuance (staging and production) |
| A forged `X-Forwarded-For` cannot dodge rate limits | DuckDNS with a real name and token |
| Secrets (`token=`, the Telegram webhook path) are redacted from Caddy logs | A real deploy over SSH from GitHub Actions |
| Backup and restore round trip against a real Postgres | `setsid -w` and `up -d --no-deps --wait` on the VM's own Docker |
| `deploy.sh` rollback logic, against stubs and throwaway git repos | Real email through an SMTP service |
| `bootstrap-vm.sh` against stubs and a fake `/etc` | UptimeRobot |
| The auto-deploy workflow's YAML and its shell logic | The Render + Neon + Upstash fallback (nothing deployed) |

**The first real deploy is the real test.** If something fails, send the assistant the output
of these, after removing anything secret (tokens, passwords, the contents of `deploy/.env.prod`,
the DuckDNS token, the SMTP password; IPs and the domain are fine):

```bash
dc ps
dc logs --since 15m caddy
dc logs --since 15m api
tail -n 100 ~/home-awareness/deploy/last-deploy.log
sudo iptables -L INPUT -n --line-numbers
```

and the exact error text from the Actions log or `deploy/bootstrap-vm.sh`. Caddy redacts
`token=` in its own log, but `dc logs api | grep token=` output is a live confirmation or
reset link: do not paste it.
