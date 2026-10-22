#!/bin/sh
set -eu

if [ "$#" -lt 1 ]; then
    echo "Usage: run-npx.sh <package> [arguments...]" >&2
    exit 64
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PACKAGE=$1
shift

# Resolve the exact direct dependency from the manifest Dependabot maintains.
VERSION=$(node -e '
    const dependencies = require(process.argv[1]).dependencies;
    const version = Object.hasOwn(dependencies, process.argv[2])
        ? dependencies[process.argv[2]] : null;
    if (typeof version !== "string" || !/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(version)) {
        console.error("Package must have an exact version in mcpo/npm/package.json");
        process.exit(64);
    }
    process.stdout.write(version);
' "$SCRIPT_DIR/npm/package.json" "$PACKAGE")

exec npx --yes "$PACKAGE@$VERSION" "$@"
