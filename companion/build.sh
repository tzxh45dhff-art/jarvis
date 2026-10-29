#!/bin/bash
# Rebuild, sign and relaunch the companion.
#
# Signing with the real Apple Development identity rather than ad-hoc is what
# makes macOS keep the Accessibility grant: TCC keys on the app's designated
# requirement, and an ad-hoc signature's requirement is its code hash — which
# changes on every single build, silently revoking permission each time.
set -e
cd "$(dirname "${BASH_SOURCE[0]}")"

IDENTITY="${JARVIS_SIGN_IDENTITY:-$(security find-identity -v -p codesigning \
  | grep -m1 'Apple Development' | sed -E 's/.*"(.*)"/\1/')}"

swift build -c release

# Refuse to ship a build whose own logic checks fail.
.build/release/JarvisCompanion --selftest
APP="build/JARVIS Companion.app"
mkdir -p "$APP/Contents/MacOS"
cp .build/release/JarvisCompanion "$APP/Contents/MacOS/JARVIS Companion"
cp Info.plist "$APP/Contents/Info.plist"

if [ -n "$IDENTITY" ]; then
  codesign --force --deep --options runtime --entitlements entitlements.plist \
    --sign "$IDENTITY" "$APP" >/dev/null 2>&1
  echo "signed as: $IDENTITY"
else
  codesign --force --deep --sign - "$APP" >/dev/null 2>&1
  echo "signed ad-hoc (no Developer identity found — Accessibility will need"
  echo "re-granting after each rebuild)"
fi

pkill -f "JARVIS Companion" 2>/dev/null || true
sleep 0.5
rsync -a --delete "$APP/" "/Applications/JARVIS Companion.app/" 2>/dev/null \
  || cp -R "$APP" /Applications/
open "/Applications/JARVIS Companion.app"
echo "relaunched"
