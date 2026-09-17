# RunPod ComfyUI bridge

Open WebUI's native ComfyUI engine only speaks real-ComfyUI protocol
(WebSocket + `/prompt` + `/history` + `/view`), while the RunPod serverless
endpoint (`worker-comfyui`) exposes `/runsync` + `/status` and returns base64
images. This tiny aiohttp bridge translates between the two, so Open WebUI can
use the RunPod endpoint as if it were a local ComfyUI server — **generation
only** (the local ComfyUI on `<your-LAN-IP>:8188` stays configured for image
*editing*).

## How it works

```
Open WebUI ──ws + POST /prompt──▶ runpod-bridge ──POST /run──▶ RunPod endpoint
            ◀──executing events── (polls /status/<job_id>)
            ◀──GET /history ────── (stores base64 output in memory)
            ◀──GET /view ──────── (serves PNG bytes)
```

- `POST /prompt` returns a `prompt_id` immediately (queue semantics), the job
  runs in a background task.
- On completion the bridge sends ComfyUI-style `executing` (node=None) events
  over the websocket; Open WebUI then fetches `/history/<prompt_id>`, which
  references `/view?prompt_id=...`.
- Results are kept in memory only (`MAX_STORED_PROMPTS`, default 64) — nothing
  is written to disk.
- `/object_info` is faked so Open WebUI can list models (from `BRIDGE_MODELS`)
  and verify the URL.

## Configuration

| Env var               | Default                        | Notes                              |
| --------------------- | ------------------------------ | ---------------------------------- |
| `RUNPOD_API_KEY`      | —                              | rpa_… key (mounted from secret)    |
| `RUNPOD_ENDPOINT_ID`  | —                              | endpoint id, e.g. `your_endpoint_id` |
| `BRIDGE_MODELS`       | `flux1-dev-fp8.safetensors`    | comma-separated model list         |
| `RUNPOD_POLL_INTERVAL`| `3`                            | seconds between status polls       |
| `RUNPOD_POLL_TIMEOUT` | `900`                          | give up after N seconds            |
| `MAX_STORED_PROMPTS`  | `64`                           | in-memory result cache             |

The workflow template is `workflow.json` (flux1-dev fp8, 4-step schnell-style
sampler settings adapted for dev). Open WebUI overrides prompt/width/height/
steps/seed/model via its node map — configure those node ids in the Open WebUI
admin UI (Settings → Images → ComfyUI workflow).

## Testing

```sh
# 1. direct endpoint smoke test (bypasses the bridge, one real generation)
KEY=$(cat /opt/secrets/runpod.key)
curl -s https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/health -H "Authorization: Bearer $KEY"

# 2. bridge E2E (mimics Open WebUI's client): ws connect -> POST /prompt ->
#    wait for executing(node=null) -> GET /history -> GET /view?filename=...
#    (view works WITHOUT prompt_id: falls back to the most recent prompt)
docker exec -i open-webui python - <<'PY'
import asyncio, json, aiohttp
BASE = 'http://runpod-bridge:8190'
async def main():
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(f'{BASE}/ws?clientId=t')
        # minimal flux workflow ...
        r = await s.post(f'{BASE}/prompt', json={'prompt': {...}, 'client_id': 't'})
        pid = (await r.json())['prompt_id']
        async for msg in ws:  # wait for completion event
            ...
PY
```

## Gotchas discovered

- RunPod names every output `ComfyUI_00001_.png` regardless of batch, so the
  bridge renames images per-index before storing; Open WebUI resolves `/view`
  by filename alone (no `prompt_id`), hence the last-prompt fallback.
- Open WebUI's ComfyUI client waits on the WebSocket for `executing` events
  with `node: null`; a plain HTTP-blocked `/prompt` would hit client timeouts.
- RunPod outputs expire after ~30 min; the bridge caches base64 in memory
  (`MAX_STORED_PROMPTS`) but there is no persistence — old generations are
  gone after a bridge restart.
- Open WebUI reads `image_generation.*` from its config DB live on every
  request; env vars only seed keys that don't exist yet. Changing the backend
  via SQL or admin UI needs no container restart.