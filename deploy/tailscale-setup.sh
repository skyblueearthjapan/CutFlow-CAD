#!/usr/bin/env bash
# CutFlow•CAD — VPS bootstrap: install Tailscale + enable Funnel.
#
# Usage (on the VPS, as root or via sudo):
#
#   bash deploy/tailscale-setup.sh
#
# Pre-requisites
#   * `tailscale login` interactively the first time on a fresh node so the
#     machine joins your tailnet; this script then idempotently re-runs
#     `tailscale up` only when the node is not yet joined (H4: never reset
#     a working identity).
#   * The container stack must already be running on port 5173 (web) — see
#     `deploy/vps-deploy.sh`.
#
# Idempotency
#   Re-running this script is safe: install is gated on `command -v
#   tailscale`, and `tailscale serve` / `funnel` overwrite existing state.
#
# Authentication note
#   Funnel exposes the URL to the public internet. There is no built-in
#   auth — pair with Cloudflare Access or basic-auth at the proxy if the
#   workshop tool must stay private (tracked for Phase 5).

set -euo pipefail

if ! command -v tailscale >/dev/null; then
  echo "==> installing tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh
fi

# H4: never run --reset on a node that already has an identity; the flag
# wipes the auth key + ACL state and forces re-authentication. We only
# call ``tailscale up`` when the node reports Logged Out / Stopped.
status_json=$(sudo tailscale status --json 2>/dev/null || echo '{}')
backend_state=$(printf '%s' "$status_json" | jq -r '.BackendState // ""')
case "$backend_state" in
  Running)
    echo "==> tailscale already up (BackendState=Running) — skipping 'tailscale up'"
    ;;
  *)
    echo "==> tailscaled is in state '${backend_state:-unknown}'; running 'tailscale up'"
    sudo tailscale up
    ;;
esac

# Publish the frontend on the tailnet's HTTPS port, then expose it via Funnel.
# We funnel ONLY port 443 because Tailscale Funnel restricts public ingress
# to 443/8443/10000 — picking 443 keeps the URL bare-domain (no :port).
echo "==> serving http://localhost:5173 on https://<host>:443"
sudo tailscale serve --bg --https=443 http://localhost:5173

# H8: the current Tailscale CLI form is ``tailscale funnel --https=443``
# pointing at a local target; the legacy ``tailscale funnel 443 on`` is
# deprecated but still accepted. We use the modern syntax so the script
# keeps working as the CLI evolves.
echo "==> enabling Funnel on 443 (public ingress)"
sudo tailscale funnel --https=443 http://localhost:5173

# Print the resulting public URL so the operator can hand it to colleagues.
MAGIC_SUFFIX=$(sudo tailscale status --json | jq -r '.MagicDNSSuffix' || true)
HOST=$(hostname)
if [[ -n "$MAGIC_SUFFIX" && "$MAGIC_SUFFIX" != "null" ]]; then
  echo ""
  echo "Tailscale Funnel enabled. URL:"
  echo "  https://${HOST}.${MAGIC_SUFFIX}/"
else
  echo ""
  echo "Tailscale Funnel enabled; run 'tailscale serve status' to see URLs."
fi
