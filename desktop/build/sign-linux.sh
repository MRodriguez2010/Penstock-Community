#!/usr/bin/env bash
set -euo pipefail
: "${LINUX_SIGNING_KEY:?Provide the release signing key}" "${LINUX_SIGNING_FINGERPRINT:?Provide the expected publisher fingerprint}"
bundle=desktop/src-tauri/target/release/bundle
export GNUPGHOME
GNUPGHOME=$(mktemp -d)
trap 'rm -rf -- "$GNUPGHOME"' EXIT
printf '%s' "$LINUX_SIGNING_KEY" | gpg --batch --import
gpg --batch --list-secret-keys --with-colons | awk -F: '$1=="fpr"{print $10}' | grep -Fxi -- "$LINUX_SIGNING_FINGERPRINT" >/dev/null
printf '%s' "${LINUX_SIGNING_PASSPHRASE:-}" | gpg --batch --pinentry-mode loopback --passphrase-fd 0 --local-user "$LINUX_SIGNING_FINGERPRINT" --armor --detach-sign "$bundle/SHA256SUMS.txt"
gpg --batch --verify "$bundle/SHA256SUMS.txt.asc" "$bundle/SHA256SUMS.txt"
(cd "$bundle" && sha256sum --check SHA256SUMS.txt)
