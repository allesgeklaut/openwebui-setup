#!/bin/sh
# Inject the Exa API key into a container-local copy of settings.yml at
# startup. The repository's settings.yml carries only the
# __EXA_API_KEY__ placeholder — the real key lives in exactly one place,
# /opt/secrets/exa.key (mounted read-only at /run/secrets/exa.key; the
# same one-place convention as litellm's /opt/secrets/mistral.env).
# The generated file lives in the container's ephemeral /tmp: it is
# never written to a volume, never to the repo, and dies with the
# container. SearXNG's settings loader honors SEARXNG_SETTINGS_PATH
# pointing at a file (searx/settings_loader.py), so the app reads the
# generated copy instead of the placeholder template.
set -eu

K="$(cat /run/secrets/exa.key)"
sed "s|__EXA_API_KEY__|$K|" /etc/searxng/settings.yml > /tmp/searxng-settings.yml
export SEARXNG_SETTINGS_PATH=/tmp/searxng-settings.yml

exec /usr/local/searxng/entrypoint.sh
