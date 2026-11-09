#!/usr/bin/env bash
# One-time bootstrap of a fresh Ubuntu 22.04/24.04 VM (written for Oracle Cloud Always Free
# Ampere A1, aarch64; x86_64 works too). Run it as the normal login user (for example `ubuntu`),
# NOT as root: individual steps use sudo.
#
#   deploy/bootstrap-vm.sh [--dry-run]
#
# Steps: Docker Engine + compose plugin from Docker's apt repository, docker group, firewall
# ports 80/tcp 443/tcp 443/udp (before Oracle's final REJECT rule, persisted across reboots),
# unattended-upgrades, a 2 GB swap file if the machine has no swap. It is idempotent: a second
# run changes nothing and says so. --dry-run prints every step as "would: ..." and runs nothing
# that changes the machine (no apt, sudo, iptables, systemctl, tee, network).
#
# Memory assumption: the Always Free Ampere A1 shape (up to 24 GB RAM). The x86 micro shapes
# (1 GB RAM) are too small for this stack; the swap file only softens that, it does not fix it.
#
# Test hooks (documented here because the tests use them; never needed in normal use):
#   BOOTSTRAP_ASSUME_UID   pretend to run as this uid (the root refusal is tested with 0)
#   BOOTSTRAP_OS_RELEASE   path of the os-release file (default /etc/os-release)
#   BOOTSTRAP_ETC          prefix for the /etc paths this script reads and writes (default /etc)
#   BOOTSTRAP_PROC_SWAPS   path of the swap list (default /proc/swaps)
set -euo pipefail
export LC_ALL=C

# Fingerprint of Docker's apt repository signing key, published at
# https://docs.docker.com/engine/install/ubuntu/ ("Docker's official GPG key", 9DC8 5822 9FC7
# DD38 854A E2D8 8D81 803C 0EBF CD88). The downloaded key is rejected unless it matches.
DOCKER_GPG_FPR="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
DOCKER_KEY_URL="https://download.docker.com/linux/ubuntu/gpg"
DOCKER_PKGS=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
MIN_COMPOSE="2.24.0" # `!reset`/`!override` in the prod overlay and `--wait-timeout`
SWAP_FILE=/swapfile

DRY=0
CHANGED=0
APT_UPDATED=0
ETC=${BOOTSTRAP_ETC:-/etc}
OS_RELEASE=${BOOTSTRAP_OS_RELEASE:-/etc/os-release}
PROC_SWAPS=${BOOTSTRAP_PROC_SWAPS:-/proc/swaps}

say() { printf '%s\n' "$*"; }
warn() { printf 'bootstrap: %s\n' "$*" >&2; }
die() {
  printf 'bootstrap: %s\n' "$*" >&2
  exit "${2:-1}"
}
would() { say "would: $*"; }
ok() { say "ok: $*"; }
did() {
  say "done: $*"
  CHANGED=1
}

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
  case $arg in
    --dry-run) DRY=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      printf 'bootstrap: unknown option: %s\n' "$arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# --- who and where -------------------------------------------------------------------------
if [[ ${BOOTSTRAP_ASSUME_UID:-$(id -u)} -eq 0 ]]; then
  die "do not run this as root. Log in as the normal user (e.g. ubuntu) and run it there; it calls sudo for the individual steps."
fi
USER_NAME=$(id -un)

# shellcheck disable=SC1090
read -r OS_ID OS_VERSION OS_CODENAME < <(
  . "$OS_RELEASE" 2>/dev/null
  echo "${ID:-unknown} ${VERSION_ID:-unknown} ${VERSION_CODENAME:-unknown}"
) || true
OS_ID=${OS_ID:-unknown}
OS_VERSION=${OS_VERSION:-unknown}
OS_CODENAME=${OS_CODENAME:-unknown}
if [[ $OS_ID != ubuntu ]]; then
  die "this script supports Ubuntu 22.04 and 24.04 only (found: $OS_ID $OS_VERSION). On other systems install Docker by hand."
fi
if [[ $OS_VERSION != 22.04 && $OS_VERSION != 24.04 ]]; then
  warn "only Ubuntu 22.04 and 24.04 are tested (found $OS_VERSION); continuing."
fi
if [[ $OS_CODENAME == unknown ]]; then
  die "cannot determine the Ubuntu release codename from $OS_RELEASE"
fi

MACHINE=$(uname -m)
case $MACHINE in
  aarch64 | arm64) DEB_ARCH=arm64 ;;
  x86_64)
    DEB_ARCH=amd64
    warn "x86_64 detected. This script targets Oracle's Ampere A1 (aarch64). The x86 micro shapes have only 1 GB of RAM, which is too little for this stack."
    ;;
  *) die "unsupported architecture: $MACHINE (need aarch64, or x86_64 with enough memory)" ;;
esac

if ((DRY == 0)); then
  command -v sudo >/dev/null 2>&1 || die "sudo is required"
fi

# --- helpers -------------------------------------------------------------------------------
version_ge() { [[ $(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n 1) == "$2" ]]; }

pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'; }

apt_update_once() {
  if ((APT_UPDATED == 0)); then
    sudo env DEBIAN_FRONTEND=noninteractive apt-get update -qq
    APT_UPDATED=1
  fi
}

apt_install() {
  apt_update_once
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
}

compose_version() {
  docker compose version --short 2>/dev/null | head -n 1 | tr -d 'v\r ' || true
}

check_compose() {
  local v
  v=$(compose_version)
  if [[ ! $v =~ ^[0-9]+\.[0-9]+ ]]; then
    die "Docker Compose plugin not found (need $MIN_COMPOSE or newer)"
  fi
  if ! version_ge "$v" "$MIN_COMPOSE"; then
    die "Docker Compose $v is too old: this stack needs $MIN_COMPOSE or newer (apt install --only-upgrade docker-compose-plugin, or re-run after adding Docker's repository)"
  fi
  ok "Docker Compose $v (>= $MIN_COMPOSE)"
}

# --- 1. Docker -----------------------------------------------------------------------------
step_docker() {
  local keyring="$ETC/apt/keyrings/docker.gpg"
  local list="$ETC/apt/sources.list.d/docker.list"
  local repo="deb [arch=$DEB_ARCH signed-by=$keyring] https://download.docker.com/linux/ubuntu $OS_CODENAME stable"
  if ((DRY)); then
    would "install ca-certificates, curl and gnupg"
    would "download Docker's apt key and verify its fingerprint is $DOCKER_GPG_FPR"
    would "add Docker's apt repository: $repo"
    would "install ${DOCKER_PKGS[*]}"
    if command -v docker >/dev/null 2>&1; then
      check_compose
    else
      would "check that Docker Compose is $MIN_COMPOSE or newer"
    fi
    return
  fi

  local missing=() p
  for p in "${DOCKER_PKGS[@]}"; do pkg_installed "$p" || missing+=("$p"); done
  if ((${#missing[@]} == 0)); then
    ok "Docker packages already installed"
    check_compose
    return
  fi

  local pre=()
  for p in ca-certificates curl gnupg; do pkg_installed "$p" || pre+=("$p"); done
  if ((${#pre[@]})); then
    apt_install "${pre[@]}"
  fi

  if [[ -f $list ]] && [[ $(<"$list") == "$repo" ]] && [[ -f $keyring ]]; then
    ok "Docker's apt repository already configured"
  else
    local tmp fpr
    tmp=$(mktemp -d)
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" RETURN
    curl -fsSL "$DOCKER_KEY_URL" -o "$tmp/docker.asc"
    fpr=$(gpg --show-keys --with-colons "$tmp/docker.asc" 2>/dev/null | awk -F: '$1 == "fpr" {print $10; exit}')
    if [[ $fpr != "$DOCKER_GPG_FPR" ]]; then
      die "Docker's apt key fingerprint does not match the published one (got: ${fpr:-none}); not installing anything"
    fi
    sudo install -m 0755 -d "$ETC/apt/keyrings"
    sudo gpg --batch --yes --dearmor -o "$keyring" "$tmp/docker.asc"
    sudo chmod a+r "$keyring"
    printf '%s\n' "$repo" | sudo tee "$list" >/dev/null
    APT_UPDATED=0
    did "added Docker's apt repository ($OS_CODENAME, $DEB_ARCH)"
  fi
  apt_install "${DOCKER_PKGS[@]}"
  did "installed ${DOCKER_PKGS[*]}"
  check_compose
}

# --- 2. docker group -----------------------------------------------------------------------
step_group() {
  if ((DRY)); then
    would "add $USER_NAME to the docker group (log out and in afterwards)"
    return
  fi
  if id -nG | tr ' ' '\n' | grep -qx docker; then
    ok "$USER_NAME is in the docker group"
  else
    sudo usermod -aG docker "$USER_NAME"
    did "added $USER_NAME to the docker group (log out and back in, or run: newgrp docker)"
  fi
}

# --- 3. firewall ---------------------------------------------------------------------------
# Oracle's Ubuntu images load iptables rules that ACCEPT SSH and then REJECT everything else,
# so a rule appended with -A would never match. Rules are inserted before that REJECT rule and
# also written into the persisted rules file (netfilter-persistent loads it at boot). The SSH
# rule is never touched, no firewall is enabled or flushed, and ufw is not used.
RULE_SPECS=("tcp 80" "tcp 443" "udp 443")

input_rules() { sudo iptables -S INPUT | awk '/^-A INPUT /'; }

reject_pos() { # 1-based position among INPUT rules of the first REJECT rule (empty if none)
  input_rules | awk '/ -j REJECT/ {print NR; exit}'
}

rule_pos() { # position of an exact ACCEPT rule (empty if absent)
  local want=$1
  input_rules | awk -v w="$want" '$0 == w {print NR; exit}'
}

step_firewall() {
  local spec proto port
  if ((DRY)); then
    for spec in "${RULE_SPECS[@]}"; do
      read -r proto port <<<"$spec"
      would "open $port/$proto: iptables -I INPUT <before the final REJECT rule> -p $proto -m $proto --dport $port -j ACCEPT (skipped if present)"
    done
    would "persist the rules for reboots (install iptables-persistent if missing; add them to $ETC/iptables/rules.v4 before its REJECT rule, backup kept as rules.v4.ha-bak)"
    return
  fi

  if ! pkg_installed iptables-persistent || ! pkg_installed netfilter-persistent; then
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables-persistent netfilter-persistent \
      || { apt_update_once && apt_install iptables-persistent netfilter-persistent; }
    did "installed iptables-persistent"
  fi

  local -a lines=()
  local rp pos
  for spec in "${RULE_SPECS[@]}"; do
    read -r proto port <<<"$spec"
    local -a spec_args=(-p "$proto" -m "$proto" --dport "$port" -j ACCEPT)
    lines+=("-A INPUT ${spec_args[*]}")
    rp=$(reject_pos)
    if sudo iptables -C INPUT "${spec_args[@]}" 2>/dev/null; then
      pos=$(rule_pos "-A INPUT ${spec_args[*]}")
      if [[ -z $rp || -z $pos || $pos -lt $rp ]]; then
        ok "$port/$proto is open"
        continue
      fi
      warn "$port/$proto is allowed only after the REJECT rule (ineffective); inserting it before"
    fi
    if [[ -n $rp ]]; then
      sudo iptables -I INPUT "$rp" "${spec_args[@]}"
    else
      sudo iptables -A INPUT "${spec_args[@]}"
    fi
    did "opened $port/$proto"
  done

  persist_rules "${lines[@]}"
}

persist_rules() {
  local f="$ETC/iptables/rules.v4" content missing=() r tmp
  if ! content=$(sudo cat "$f" 2>/dev/null); then
    sudo netfilter-persistent save
    did "saved the current rules to $f"
    return
  fi
  for r in "$@"; do
    grep -qxF -- "$r" <<<"$content" || missing+=("$r")
  done
  if ((${#missing[@]} == 0)); then
    ok "the rules are persisted in $f"
    return
  fi
  if ! grep -q '^-A INPUT .*-j REJECT' <<<"$content"; then
    # No REJECT line to anchor on: save the live rules (which already contain ours).
    sudo netfilter-persistent save
    did "saved the current rules to $f"
    return
  fi
  [[ -e $f.ha-bak ]] || sudo cp -p "$f" "$f.ha-bak"
  tmp=$(mktemp)
  awk -v rules="$(printf '%s\n' "${missing[@]}")" \
    '!done && /^-A INPUT .*-j REJECT/ {print rules; done=1} {print}' <<<"$content" >"$tmp"
  sudo install -m 0644 "$tmp" "$f"
  rm -f "$tmp"
  did "added the rules to $f before its REJECT rule"
}

# --- 4. unattended-upgrades ----------------------------------------------------------------
step_upgrades() {
  local conf="$ETC/apt/apt.conf.d/20auto-upgrades"
  if ((DRY)); then
    would "install unattended-upgrades and enable it ($conf: daily package lists and upgrades)"
    return
  fi
  if pkg_installed unattended-upgrades; then
    ok "unattended-upgrades is installed"
  else
    apt_install unattended-upgrades
    did "installed unattended-upgrades"
  fi
  if grep -qs 'APT::Periodic::Update-Package-Lists "1"' "$conf" \
    && grep -qs 'APT::Periodic::Unattended-Upgrade "1"' "$conf"; then
    ok "unattended-upgrades is enabled"
  else
    printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
      | sudo tee "$conf" >/dev/null
    did "enabled unattended-upgrades in $conf"
  fi
  if [[ $(systemctl is-enabled unattended-upgrades 2>/dev/null || true) == enabled ]]; then
    ok "the unattended-upgrades service is enabled"
  else
    sudo systemctl enable --now unattended-upgrades
    did "enabled the unattended-upgrades service"
  fi
}

# --- 5. swap -------------------------------------------------------------------------------
step_swap() {
  if ((DRY)); then
    would "create a 2 GB swap file at $SWAP_FILE and add it to $ETC/fstab (only if the machine has no swap)"
    return
  fi
  if (($(wc -l <"$PROC_SWAPS") > 1)); then
    ok "swap already present"
    return
  fi
  if [[ -e $SWAP_FILE ]]; then
    warn "$SWAP_FILE exists but is not active swap; leaving it alone (activate it by hand: sudo swapon $SWAP_FILE)"
    return
  fi
  sudo dd if=/dev/zero of="$SWAP_FILE" bs=1M count=2048 status=none
  sudo chmod 600 "$SWAP_FILE"
  sudo mkswap "$SWAP_FILE" >/dev/null
  sudo swapon "$SWAP_FILE"
  if ! grep -qs "^$SWAP_FILE " "$ETC/fstab"; then
    printf '%s none swap sw 0 0\n' "$SWAP_FILE" | sudo tee -a "$ETC/fstab" >/dev/null
  fi
  did "created and enabled a 2 GB swap file"
}

# --- 7. what is left to do by hand ---------------------------------------------------------
print_next_steps() {
  cat <<'NEXT'

Remaining manual steps
  1. In the Oracle Cloud console, allow ingress to this VM: VCN > Security List (or the
     instance's Network Security Group) > add rules for TCP 80, TCP 443 and UDP 443 from
     0.0.0.0/0. The host firewall is open now, but the cloud firewall is separate.
  2. Log out and back in so the docker group applies (or run: newgrp docker).
  3. Clone the repository:   git clone <repo-url> ~/home-awareness && cd ~/home-awareness
  4. Create the secrets:     python3 scripts/gen_secrets.py     (writes deploy/.env.prod, mode 0600)
  5. Edit deploy/.env.prod: set DOMAIN and ACME_EMAIL (and DUCKDNS_TOKEN with
     COMPOSE_PROFILES=duckdns if you use a free DuckDNS name).
  6. First deploy:           deploy/deploy.sh
  7. Optional: add a free UptimeRobot monitor for https://<DOMAIN>/health (see deploy/README.md).
NEXT
}

main() {
  if ((DRY)); then
    say "bootstrap: dry run on Ubuntu $OS_VERSION ($OS_CODENAME), $MACHINE; nothing will be changed"
  else
    say "bootstrap: Ubuntu $OS_VERSION ($OS_CODENAME), $MACHINE, user $USER_NAME"
  fi
  step_docker
  step_group
  step_firewall
  step_upgrades
  step_swap
  if ((DRY)); then
    say ""
    say "bootstrap: dry run finished; nothing was changed"
  elif ((CHANGED == 0)); then
    say ""
    say "bootstrap: nothing to change, this machine is already bootstrapped"
  else
    say ""
    say "bootstrap: finished"
  fi
  print_next_steps
}

main
