#!/bin/sh
# Quick smoke test of the RunPod endpoint directly (bypasses the bridge).
# Reads RUNPOD_API_KEY / RUNPOD_ENDPOINT_ID from the environment.
set -e

: "${RUNPOD_API_KEY:?set RUNPOD_API_KEY}"
: "${RUNPOD_ENDPOINT_ID:?set RUNPOD_ENDPOINT_ID}"

BASE="https://api.runpod.ai/v2/${RUNPOD_ENDPOINT_ID}"
WORKFLOW_FILE="${1:-/data/workflow.json}"

RESP=$(curl -s -X POST "$BASE/run" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${RUNPOD_API_KEY}" \
  -d "{\"input\":{\"workflow\":$(cat "$WORKFLOW_FILE")}}")

echo "$RESP"

JID=$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')
[ -n "$JID" ] || exit 1

for i in $(seq 1 100); do
  sleep 3
  ST=$(curl -s "$BASE/status/$JID" -H "Authorization: Bearer ${RUNPOD_API_KEY}")
  S=$(echo "$ST" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')
  echo "[$i] status=$S"
  case "$S" in
    COMPLETED|FAILED|CANCELLED) echo "$ST" | head -c 2000; echo; exit 0 ;;
  esac
done