#!/usr/bin/env bash
# Keep trying to create the Always Free Arm VM until Oracle has capacity.
#
# Run this in OCI Cloud Shell (the terminal icon at the top right of the Oracle console):
# it is already logged in, so no keys or passwords are needed. Paste your SSH PUBLIC key
# into ~/home-awareness.pub first (see docs/deploy-oracle.md). Nothing secret is used here.
#
#   bash oracle-launch-retry.sh
#
# Settings can be overridden with environment variables, for example OCPUS=2 MEMORY_GB=12.
# It tries every availability domain, waits 4 to 7 minutes between rounds, never creates a
# second VM (it stops if one named $NAME already exists), and only retries on "out of
# capacity"; any other error stops it with the message so you can read it.
# The JMESPath queries below use backticks on purpose (literal values), not command substitution.
# shellcheck disable=SC2016
set -uo pipefail

NAME="${NAME:-home-awareness}"
OCPUS="${OCPUS:-1}"
MEMORY_GB="${MEMORY_GB:-6}"
BOOT_GB="${BOOT_GB:-50}"
SHAPE="VM.Standard.A1.Flex"
KEY_FILE="${KEY_FILE:-$HOME/home-awareness.pub}"
COMPARTMENT="${COMPARTMENT:-${OCI_TENANCY:-}}"   # the root compartment unless you set it

say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

[ -n "$COMPARTMENT" ] || { say "No compartment: set COMPARTMENT=<ocid> (OCI_TENANCY is empty)."; exit 2; }
[ -s "$KEY_FILE" ] || { say "Put your SSH PUBLIC key (one line, starts with ssh-ed25519 or ssh-rsa) in $KEY_FILE first."; exit 2; }
grep -qE '^(ssh-(ed25519|rsa)|ecdsa-sha2-) ' "$KEY_FILE" || { say "$KEY_FILE does not look like a public key. Never paste a PRIVATE key."; exit 2; }
command -v oci >/dev/null || { say "The oci command was not found. Run this inside OCI Cloud Shell."; exit 2; }

say "Looking up the Ubuntu 24.04 Arm image..."
IMAGE="$(oci compute image list --compartment-id "$COMPARTMENT" --operating-system "Canonical Ubuntu" \
  --operating-system-version "24.04" --shape "$SHAPE" --sort-by TIMECREATED --sort-order DESC \
  --query 'data[0].id' --raw-output 2>/dev/null)"
[ -n "$IMAGE" ] && [ "$IMAGE" != "null" ] || { say "Could not find the Ubuntu 24.04 Arm image."; exit 2; }

say "Looking up a PUBLIC subnet (one that allows public IP addresses)..."
SUBNET="$(oci network subnet list --compartment-id "$COMPARTMENT" \
  --query 'data[?"prohibit-public-ip-on-vnic"==`false`] | [0].id' --raw-output 2>/dev/null)"
[ -n "$SUBNET" ] && [ "$SUBNET" != "null" ] || {
  say "No public subnet found. Create one first: Networking > Virtual cloud networks > Start VCN Wizard > 'Create VCN with Internet Connectivity'."
  exit 2
}

mapfile -t ADS < <(oci iam availability-domain list --compartment-id "$COMPARTMENT" --query 'data[].name' --raw-output 2>/dev/null | tr -d '[]", ' | sed '/^$/d')
[ "${#ADS[@]}" -gt 0 ] || { say "Could not list availability domains."; exit 2; }
say "Availability domains: ${ADS[*]}"
say "Will create $NAME: $SHAPE, $OCPUS OCPU, ${MEMORY_GB} GB, ${BOOT_GB} GB disk, public IP on."

attempt=0
while true; do
  existing="$(oci compute instance list --compartment-id "$COMPARTMENT" --display-name "$NAME" \
    --query 'data[?"lifecycle-state"!=`TERMINATED`] | [0].id' --raw-output 2>/dev/null)"
  if [ -n "$existing" ] && [ "$existing" != "null" ]; then
    say "An instance named $NAME already exists ($existing). Stopping so a second one isn't created."
    break
  fi

  attempt=$((attempt + 1))
  done_ok=0
  for ad in "${ADS[@]}"; do
    say "Attempt $attempt in $ad ..."
    out="$(oci compute instance launch \
      --availability-domain "$ad" --compartment-id "$COMPARTMENT" \
      --shape "$SHAPE" --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEMORY_GB}" \
      --image-id "$IMAGE" --subnet-id "$SUBNET" --assign-public-ip true \
      --display-name "$NAME" --ssh-authorized-keys-file "$KEY_FILE" \
      --boot-volume-size-in-gbs "$BOOT_GB" 2>&1)"
    rc=$?
    if [ "$rc" -eq 0 ]; then
      say "SUCCESS: the VM was created."
      id="$(printf '%s' "$out" | grep -m1 '"id"' | sed 's/.*"id": *"\([^"]*\)".*/\1/')"
      say "Instance: $id"
      say "It takes a minute or two to start. Then find its public IP under Compute > Instances,"
      say "and continue with docs/deploy-oracle.md (reserve the IP, open ports 80 and 443)."
      done_ok=1
      break
    fi
    if printf '%s' "$out" | grep -qiE 'out of (host )?capacity|OutOfHostCapacity'; then
      say "No capacity in $ad right now."
    else
      say "Stopping: this is not a capacity problem. Oracle said:"
      printf '%s\n' "$out" | head -20
      exit 1
    fi
  done
  [ "$done_ok" -eq 1 ] && break

  wait_s=$((240 + RANDOM % 180))
  say "Waiting $((wait_s / 60)) min $((wait_s % 60)) s before the next round (Ctrl-C to stop)."
  sleep "$wait_s"
done
