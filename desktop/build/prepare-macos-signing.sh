#!/usr/bin/env bash
set -euo pipefail
: "${APPLE_CERTIFICATE:?}" "${APPLE_CERTIFICATE_PASSWORD:?}" "${APPLE_SIGNING_IDENTITY:?}"
keychain="$RUNNER_TEMP/penstock-signing.keychain-db"
password=$(openssl rand -hex 32)
p12=$(mktemp "$RUNNER_TEMP/penstock-publisher.XXXXXX")
trap 'rm -f -- "$p12"' EXIT
printf '%s' "$APPLE_CERTIFICATE" | base64 -D > "$p12"
security create-keychain -p "$password" "$keychain"
security unlock-keychain -p "$password" "$keychain"
security set-keychain-settings -lut 7200 "$keychain"
security import "$p12" -k "$keychain" -P "$APPLE_CERTIFICATE_PASSWORD" -T /usr/bin/codesign
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$password" "$keychain" >/dev/null
# This is a disposable GitHub-hosted signing runner.
security list-keychains -d user -s "$keychain" "$HOME/Library/Keychains/login.keychain-db"
security find-identity -v -p codesigning "$keychain" | grep -F -- "$APPLE_SIGNING_IDENTITY" >/dev/null
# PyInstaller collects native libraries as resources, outside the usual Frameworks directory.
while IFS= read -r -d '' binary; do
  case "$(file -b "$binary")" in
    *Mach-O*) codesign --force --options runtime --timestamp --sign "$APPLE_SIGNING_IDENTITY" "$binary" ;;
  esac
done < <(find desktop/src-tauri/stage -type f -print0)
