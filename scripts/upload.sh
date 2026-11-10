#!/usr/bin/env bash
# Upload an .ipa you built on your Mac straight into the source.
#   SIDESTASH_URL=http://192.168.1.10:8686 SIDESTASH_TOKEN=... ./upload.sh path/to/App.ipa ["optional notes"]
# Use the LAN address: big IPAs often exceed reverse-proxy / Cloudflare upload limits.
set -euo pipefail
: "${SIDESTASH_URL:?set SIDESTASH_URL, e.g. http://192.168.1.10:8686}"
: "${SIDESTASH_TOKEN:?set SIDESTASH_TOKEN to the ADMIN_TOKEN from the container}"
ipa="${1:?usage: upload.sh file.ipa [notes]}"
curl --fail-with-body --progress-bar \
  -H "Authorization: Bearer ${SIDESTASH_TOKEN}" \
  -F "file=@${ipa}" -F "notes=${2:-}" \
  "${SIDESTASH_URL%/}/api/upload" | python3 -m json.tool
