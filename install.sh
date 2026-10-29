#!/bin/bash
# JARVIS installer — puts `jarvis` on your PATH.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-$HOME/.local/bin}"
mkdir -p "$TARGET"
ln -sf "$DIR/bin/jarvis" "$TARGET/jarvis"
echo "linked $TARGET/jarvis -> $DIR/bin/jarvis"
case ":$PATH:" in
  *":$TARGET:"*) echo "✓ $TARGET is already on your PATH" ;;
  *) echo
     echo "Add this to ~/.zshrc:"
     echo "    export PATH=\"$TARGET:\$PATH\"" ;;
esac
echo
echo "Next:  jarvis doctor    # check permissions and models"
echo "       jarvis           # start"
