#!/bin/sh
# Pre-start script: generate mcpo/config.json from mcpo/template_config.json
# with environment variables substituted. Run on the host to start all services.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$SCRIPT_DIR/mcpo/template_config.json"
OUTPUT="$SCRIPT_DIR/mcpo/config.json"

if [ ! -f "$TEMPLATE" ]; then
  echo "Error: template_config.json not found at $TEMPLATE"
  exit 1
fi

# Load .env from project root if it exists
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
