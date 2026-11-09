# Deployment

## Notes

- Processes running on the VM itself can reach the api container directly (its address on the
  compose network), bypassing Caddy and its `X-Forwarded-For` overwrite. That is acceptable on a
  single-user VM; do not run untrusted code on it.
- Clients arriving over IPv6 on a host where containers have no IPv6 would all appear with the
  docker gateway address and share one rate-limit bucket. Oracle Always Free VMs have no IPv6 by
  default, so this does not apply unless you enable it.
