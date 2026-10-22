#!/bin/sh
# Pre-start script: generate the secret config from mcpo/template_config.json
# with environment variables substituted. Run on the host to start all services.

set -eu
umask 077

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$SCRIPT_DIR/mcpo/template_config.json"
OUTPUT="/opt/secrets/mcpo-config.json"

if [ ! -f "$TEMPLATE" ]; then
  echo "Error: template_config.json not found at $TEMPLATE"
  exit 1
fi

# Load secrets from /opt/secrets (outside the MCP filesystem server's reach),
# then non-secret config from the local .env. The renderer sees both.
# Note: mcpo has no secret-file support, so rendering writes real values into
# the generated config.json (mounted read-only into the container).
SECRETS_DIR="/opt/secrets"
for SECRETS_FILE in "$SECRETS_DIR/webui.env" "$SECRETS_DIR/trainlocks.env" "$SECRETS_DIR/mistral.env"; do
  if [ -f "$SECRETS_FILE" ]; then
    set -a
    . "$SECRETS_FILE"
    set +a
  fi
done

TRILIUM_API_KEY=$(cat /opt/secrets/trilium.token)
export TRILIUM_API_KEY

ENV_FILE="$SCRIPT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

TEMP_CONFIG=$(mktemp "$SECRETS_DIR/mcpo-config.XXXXXX")
trap 'rm -f "$TEMP_CONFIG"' EXIT
trap 'exit 1' HUP INT TERM
python3 -S "$SCRIPT_DIR/mcpo/render_config.py" "$TEMPLATE" > "$TEMP_CONFIG"
# Keep the existing inode: a running container may bind-mount this file.
# Render/validate first so a failed substitution never truncates the live config.
cat "$TEMP_CONFIG" > "$OUTPUT"
chmod 600 "$OUTPUT"
echo "Generated $OUTPUT from $TEMPLATE"

# Start all services
cd "$SCRIPT_DIR" && docker compose up -d
