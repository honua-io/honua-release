#!/usr/bin/env bash
# Write a throwaway self-signed TLS certificate for the local candidate's HTTPS endpoint
# (compose.https.yml) and print the directory that holds it.
#
# Usage: tls-cert.sh DIR
#
# The key is a test fixture for one local run, never a credential: it lives only in DIR, it is
# valid for two days and for localhost only. The server runs as a non-root user, so the files are
# world-readable inside DIR.
set -euo pipefail

dir="${1:?usage: tls-cert.sh DIR}"
mkdir -p "$dir"
# Regenerate when missing or expiring within an hour, so a reused local directory never serves a
# stale certificate.
if [ ! -s "$dir/tls.crt" ] || [ ! -s "$dir/tls.key" ] \
  || ! openssl x509 -checkend 3600 -noout -in "$dir/tls.crt" >/dev/null 2>&1; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
    -keyout "$dir/tls.key" -out "$dir/tls.crt" >/dev/null 2>&1
fi
chmod 0755 "$dir"
chmod 0644 "$dir/tls.crt" "$dir/tls.key"
cd "$dir" && pwd -P
