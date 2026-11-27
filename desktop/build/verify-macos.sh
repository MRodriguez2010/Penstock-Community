#!/usr/bin/env bash
set -euo pipefail
: "${APPLE_ID:?}" "${APPLE_PASSWORD:?}" "${APPLE_TEAM_ID:?}" "${APPLE_SIGNING_IDENTITY:?}"
root=desktop/src-tauri/target/release/bundle
app="$root/macos/Penstock Community.app"
codesign --verify --deep --strict "$app"
codesign -dv --verbose=4 "$app" 2>&1 | grep -Fx "TeamIdentifier=$APPLE_TEAM_ID" >/dev/null
xcrun stapler validate "$app"
spctl --assess --type execute "$app"
shopt -s nullglob
images=("$root"/dmg/*.dmg)
(( ${#images[@]} > 0 ))
for dmg in "${images[@]}"; do
  xcrun notarytool submit "$dmg" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_PASSWORD" --wait --timeout 30m
  xcrun stapler staple "$dmg"
  xcrun stapler validate "$dmg"
done
