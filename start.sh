#!/bin/sh
# Pre-start script: generate mcpo/config.json from mcpo/template_config.json
# with environment variables substituted. Run on the host to start all services.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$SCRIPT_DIR/mcpo/template_config.json"
OUTPUT="/opt/secrets/mcpo-config.json"

if [ ! -f "$TEMPLATE" ]; then
  echo "Error: template_config.json not found at $TEMPLATE"
  exit 1
fi

# Load secrets from /opt/secrets (outside the MCP filesystem server's reach),
# then non-secret config from the local .env. envsubst sees both.
SECRETS_DIR="/opt/secrets"
for SECRETS_FILE in "$SECRETS_DIR/webui.env" "$SECRETS_DIR/portainer.env"; do
  if [ -f "$SECRETS_FILE" ]; then
    set -a
    . "$SECRETS_FILE"
    set +a
  fi
done

ENV_FILE="$SCRIPT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

envsubst < "$TEMPLATE" > "$OUTPUT"
echo "Generated $OUTPUT from $TEMPLATE"

# Start all services
cd "$SCRIPT_DIR" && docker compose up -d
