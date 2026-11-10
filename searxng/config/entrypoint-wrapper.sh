#!/bin/sh
# Inject secrets into a container-local copy of settings.yml at startup.
# The repository's settings.yml carries only placeholders — the real values
# live in exactly one place each, /opt/secrets (mounted read-only at
# /run/secrets/*; the same one-place convention as litellm's
# /opt/secrets/mistral.env):
#   __EXA_API_KEY__  <- /opt/secrets/exa.key            (keyed exaapi engine)
#   __SCOUT_TOKEN__  <- /opt/secrets/research-search.token (web scout engine)
# The generated file lives in the container's ephemeral /tmp: it is
# never written to a volume, never to the repo, and dies with the
# container. SearXNG's settings loader honors SEARXNG_SETTINGS_PATH
# pointing at a file (searx/settings_loader.py), so the app reads the
# generated copy instead of the placeholder template.
set -eu

K="$(cat /run/secrets/exa.key)"
S="$(cat /run/secrets/research-search.token)"
sed -e "s|__EXA_API_KEY__|$K|" -e "s|__SCOUT_TOKEN__|$S|" \
  /etc/searxng/settings.yml > /tmp/searxng-settings.yml
export SEARXNG_SETTINGS_PATH=/tmp/searxng-settings.yml

exec /usr/local/searxng/entrypoint.sh
