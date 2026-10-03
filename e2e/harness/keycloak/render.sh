#!/usr/bin/env bash
# Render the S4 identity provider's per-run material: a throwaway TLS certificate and the realm
# import. Nothing here is committed - the template carries no secret, the certificate is minted for
# this run only, and the output directory is deleted when the S4 stack goes down.
#
# Usage: render.sh OUT_DIR
#   OUT_DIR/certs/kc.crt, kc.key   - Keycloak HTTPS identity; the server trusts kc.crt (SSL_CERT_FILE)
#   OUT_DIR/import/honua-realm.json - realm `honua`, confidential PKCE client `honua-console-bff`
#
# Inputs (env, all required unless noted):
#   S4_IDP_HOSTNAME         hostname the browser and the server both use for the IdP. The SAN always
#                           covers host.docker.internal, localhost and 127.0.0.1 as well, so one
#                           certificate works from the server container, the host browser and the
#                           host-run Console (issuer parity).
#   S4_CONSOLE_ORIGIN       http://127.0.0.1:<live port>; the only registered redirect is
#                           <origin>/admin/auth/callback.
#   S4_CLIENT_SECRET        confidential client secret the server is configured with.
#   S4_OPERATOR_USER / S4_OPERATOR_PASSWORD   the operator the suite signs in as (realm role admin).
#   S4_ACCESS_TOKEN_LIFESPAN  optional, seconds, default 7200. The suite runs ~10 minutes on one
#                           sign-in, and the operator bearer lives 120 minutes, so a shorter token
#                           would expire mid-run; anything below 7200 is refused.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:?usage: render.sh OUT_DIR}"

: "${S4_IDP_HOSTNAME:?}" "${S4_CONSOLE_ORIGIN:?}" "${S4_CLIENT_SECRET:?}"
: "${S4_OPERATOR_USER:?}" "${S4_OPERATOR_PASSWORD:?}"
LIFESPAN="${S4_ACCESS_TOKEN_LIFESPAN:-7200}"
if ! [[ "$LIFESPAN" =~ ^[0-9]+$ ]] || [ "$LIFESPAN" -lt 7200 ]; then
  echo "render.sh: access-token lifespan must be at least 7200 seconds (got '$LIFESPAN')" >&2
  exit 2
fi

mkdir -p "$OUT/certs" "$OUT/import"

san="DNS:host.docker.internal,DNS:localhost,IP:127.0.0.1"
case "$S4_IDP_HOSTNAME" in
  host.docker.internal|localhost|127.0.0.1) ;;
  *[!0-9.]*) san="$san,DNS:$S4_IDP_HOSTNAME" ;;
  *) san="$san,IP:$S4_IDP_HOSTNAME" ;;
esac
# Two days is enough for any run and short enough that a leaked key is worthless.
openssl req -x509 -newkey rsa:2048 -sha256 -days 2 -nodes \
  -keyout "$OUT/certs/kc.key" -out "$OUT/certs/kc.crt" \
  -subj "/CN=$S4_IDP_HOSTNAME" -addext "subjectAltName=$san" 2>/dev/null
# Keycloak runs as uid 1000 inside its container and must read this test-only key.
chmod 0644 "$OUT/certs/kc.key" "$OUT/certs/kc.crt"

jq --arg origin "$S4_CONSOLE_ORIGIN" \
   --arg secret "$S4_CLIENT_SECRET" \
   --arg user "$S4_OPERATOR_USER" \
   --arg password "$S4_OPERATOR_PASSWORD" \
   --argjson lifespan "$LIFESPAN" '
  .accessTokenLifespan = $lifespan
  | .users[0].username = $user
  | .users[0].credentials[0].value = $password
  | .clients[0].secret = $secret
  | .clients[0].redirectUris = [$origin + "/admin/auth/callback"]
  | .clients[0].webOrigins = [$origin]
  | .clients[0].attributes["post.logout.redirect.uris"] = ($origin + "/admin")
' "$HERE/honua-realm.template.json" > "$OUT/import/honua-realm.json"
chmod 0644 "$OUT/import/honua-realm.json"
