#!/bin/sh
# Run inside Open Terminal as its normal user; never during container startup.
set -eu
VERSION=2.2.6
PREFIX="$HOME/.local/share/opencode-cli/$VERSION"
LINK="$HOME/.local/bin/opencode"
# Validate the existing launcher before downloading or modifying anything.
if [ -e "$LINK" ] || [ -L "$LINK" ]; then
    if [ ! -L "$LINK" ]; then
        echo "Refusing to replace an existing non-symlink OpenCode executable" >&2
        exit 1
    fi
    case "$(readlink "$LINK")" in
        "$HOME/.local/share/opencode-cli/"*/bin/opencode) ;;
        *) echo "Refusing to replace an unmanaged OpenCode symlink" >&2; exit 1 ;;
    esac
fi
mkdir -p "$HOME/.local/bin"
EXPECTED="opencode v$VERSION"
if [ "$("$PREFIX/bin/opencode" --version 2>/dev/null || true)" != "$EXPECTED" ]; then
    npm install --global --prefix "$PREFIX" --no-audit --no-fund "@opencode/cli@$VERSION"
fi
ACTUAL="$("$PREFIX/bin/opencode" --version)"
if [ "$ACTUAL" != "$EXPECTED" ]; then
    echo "Unexpected OpenCode version; leaving launcher unchanged" >&2
    exit 1
fi
# Replace only the launcher, atomically, after the candidate has passed checks.
TEMP_DIR=$(mktemp -d "$HOME/.local/bin/.opencode-install.XXXXXX")
trap 'rm -f "$TEMP_DIR/opencode"; rmdir "$TEMP_DIR"' EXIT
ln -s "$PREFIX/bin/opencode" "$TEMP_DIR/opencode"
mv -fT "$TEMP_DIR/opencode" "$LINK"
printf '%s\n' "$ACTUAL"
