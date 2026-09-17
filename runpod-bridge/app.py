#!/usr/bin/env python3
"""RunPod ComfyUI bridge.

Exposes the subset of the native ComfyUI HTTP+WebSocket API that Open WebUI's
ComfyUI image engine uses (POST /prompt, /ws, /history/<id>, /view, /object_info)
and translates queued workflows into RunPod serverless jobs against a
worker-comfyui endpoint (POST /run, poll GET /status/<id>).

Flow mirrors ComfyUI's queue semantics: POST /prompt returns a prompt_id
immediately, the RunPod job runs in a background task, and the websocket
client receives ComfyUI-style `executing` completion events so Open WebUI
proceeds to fetch /history and /view.
"""

import asyncio
import base64
import json
import logging
import os
import time
import urllib.parse
import uuid

import aiohttp
from aiohttp import web

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("runpod-bridge")

RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")
ENDPOINT_ID = os.environ.get("RUNPOD_ENDPOINT_ID", "")
# Optional dedicated endpoint for edit jobs (workflows with input images).
# Falls back to ENDPOINT_ID when unset.
ENDPOINT_ID_EDIT = os.environ.get("RUNPOD_ENDPOINT_ID_EDIT", "")
BASE = f"https://api.runpod.ai/v2/{ENDPOINT_ID}"
BASE_EDIT = f"https://api.runpod.ai/v2/{ENDPOINT_ID_EDIT}" if ENDPOINT_ID_EDIT else BASE
POLL_INTERVAL = float(os.environ.get("RUNPOD_POLL_INTERVAL", "3"))
POLL_TIMEOUT = float(os.environ.get("RUNPOD_POLL_TIMEOUT", "900"))
MODELS = [
    m.strip()
    for m in os.environ.get("BRIDGE_MODELS", "flux1-dev-fp8.safetensors").split(",")
    if m.strip()
]
MAX_STORED_PROMPTS = int(os.environ.get("MAX_STORED_PROMPTS", "64"))
MAX_STORED_UPLOADS = int(os.environ.get("MAX_STORED_UPLOADS", "32"))

API_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {RUNPOD_API_KEY}",
}


def fake_object_info() -> dict:
    return {
        "CheckpointLoaderSimple": {
            "input": {"required": {"ckpt_name": [MODELS, {}]}},
            "output": ["MODEL", "CLIP", "VAE"],
            "output_name": ["MODEL", "CLIP", "VAE"],
        }
    }


# ---------------------------------------------------------------- RunPod client


async def runpod_submit(
    session: aiohttp.ClientSession, workflow: dict, input_images: list[dict] | None = None
) -> tuple[str, str]:
    input_payload: dict = {"workflow": workflow}
    if input_images:
        input_payload["images"] = input_images
    # Edit jobs (input images) can target a dedicated endpoint
    base = BASE_EDIT if input_images else BASE
    async with session.post(
        f"{base}/run", json={"input": input_payload}, headers=API_HEADERS
    ) as r:
        r.raise_for_status()
        res = await r.json()
    jid = res.get("id")
    if not jid:
        raise RuntimeError(f"RunPod /run returned no id: {json.dumps(res)[:300]}")
    return jid, base


async def runpod_wait(session: aiohttp.ClientSession, jid: str, base: str) -> dict:
    deadline = time.monotonic() + POLL_TIMEOUT
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            async with session.get(f"{base}/status/{jid}", headers=API_HEADERS) as r:
                r.raise_for_status()
                res = await r.json()
        except aiohttp.ClientError as e:
            log.warning("status poll error for %s: %s", jid, e)
            continue
        if res.get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
            return res
    raise TimeoutError(f"RunPod job {jid} timed out after {POLL_TIMEOUT}s")


def extract_base64(status_payload: dict) -> list[str]:
    out = status_payload.get("output") or {}
    imgs = [
        img["data"]
        for img in out.get("images", [])
        if img.get("type") == "base64" and img.get("data")
    ]
    if not imgs:
        raise RuntimeError(
            f"RunPod output had no base64 images: {json.dumps(status_payload)[:500]}"
        )
    return imgs


# ---------------------------------------------------------------- job execution


def save_node_id(workflow: dict) -> str:
    """First SaveImage/PreviewImage node id.

    Open WebUI only reads /history outputs whose node id exists in the
    submitted workflow AND is a SaveImage/PreviewImage node, so the result must
    be reported under the workflow's own node id rather than a fixed one.
    """
    for nid, node in workflow.items():
        if node.get("class_type") in ("SaveImage", "PreviewImage"):
            return nid
    return "9"


def _collect_input_images(app: web.Application, workflow: dict) -> list[dict]:
    """Find LoadImage nodes referencing uploaded files; return RunPod input.images."""
    out = []
    for node in workflow.values():
        if node.get("class_type") == "LoadImage":
            name = node.get("inputs", {}).get("image")
            if name and name in app["uploads"]:
                out.append({"name": name, "image": app["uploads"][name]})
    return out


async def execute_job(app: web.Application, prompt_id: str, workflow: dict, client_id: str):
    """Run the workflow on RunPod, store results, notify ws clients."""
    images: list[tuple[str, str]] = []  # (filename, base64)
    try:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            input_images = _collect_input_images(app, workflow)
            jid, base = await runpod_submit(session, workflow, input_images)
            log.info(
                "prompt %s -> runpod job %s (%d input image(s))",
                prompt_id,
                jid,
                len(input_images),
            )
            status = await runpod_wait(session, jid, base)
        if status.get("status") != "COMPLETED":
            raise RuntimeError(f"RunPod job {status.get('status')}: {json.dumps(status)[:500]}")
        raw = extract_base64(status)
        # Distinct filenames per image (Open WebUI resolves /view by filename)
        images = [(f"ComfyUI_{i + 1:05d}_.png", b64) for i, b64 in enumerate(raw)]
        app["images"][prompt_id] = {"node": save_node_id(workflow), "images": images}
        app["last_prompt_id"] = prompt_id
        log.info("prompt %s completed with %d image(s)", prompt_id, len(images))
    except Exception as e:
        log.exception("prompt %s failed: %s", prompt_id, e)
        app["errors"][prompt_id] = str(e)

    # prune old entries (insertion order: keys() preserves it)
    if len(app["images"]) > MAX_STORED_PROMPTS:
        for k in list(app["images"].keys())[: len(app["images"]) - MAX_STORED_PROMPTS]:
            app["images"].pop(k, None)
        if app["last_prompt_id"] not in app["images"]:
            app["last_prompt_id"] = max(app["images"].keys(), default=None)
    if len(app["errors"]) > MAX_STORED_PROMPTS:
        for k in list(app["errors"].keys())[: len(app["errors"]) - MAX_STORED_PROMPTS]:
            app["errors"].pop(k, None)

    # notify the ws client that queued this prompt (ComfyUI-style events)
    for c in list(app["ws_clients"]):
        if c.client_id == client_id and not c.ws.closed:
            try:
                if images:
                    await c.ws.send_json(
                        {
                            "type": "executing",
                            "data": {"node": "9", "display_node": "9", "prompt_id": prompt_id},
                        }
                    )
                await c.ws.send_json(
                    {"type": "executing", "data": {"node": None, "display_node": None, "prompt_id": prompt_id}}
                )
                await c.ws.send_json(
                    {"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 0}}, "sid": None}}
                )
            except Exception:
                log.exception("failed to notify ws client %s", client_id)


# ---------------------------------------------------------------- handlers


async def handle_object_info(request: web.Request) -> web.Response:
    return web.json_response(fake_object_info())


async def handle_prompt(request: web.Request) -> web.Response:
    body = await request.json()
    workflow = body.get("prompt") or {}
    client_id = body.get("client_id", "unknown")
    if not workflow:
        return web.json_response({"error": "missing prompt"}, status=400)
    prompt_id = str(uuid.uuid4())
    log.info("queued prompt %s (client %s)", prompt_id, client_id)
    asyncio.create_task(execute_job(request.app, prompt_id, workflow, client_id))
    return web.json_response({"prompt_id": prompt_id, "number": 1, "node_errors": {}})


async def handle_history(request: web.Request) -> web.Response:
    prompt_id = request.match_info["prompt_id"]
    entry = request.app["images"].get(prompt_id)
    if entry:
        return web.json_response(
            {
                prompt_id: {
                    "prompt": [],
                    "outputs": {
                        entry["node"]: {
                            "images": [
                                {"filename": fn, "subfolder": "", "type": "output"}
                                for fn, _ in entry["images"]
                            ]
                        }
                    },
                    "status": {"completed": True},
                }
            }
        )
    return web.json_response({})


async def handle_view(request: web.Request) -> web.Response:
    q = request.rel_url.query
    prompt_id = q.get("prompt_id", "")
    filename = q.get("filename", "")
    entry = request.app["images"].get(prompt_id)
    if not entry and not prompt_id:
        # Open WebUI fetches /view by filename only; fall back to the most
        # recent prompt (real ComfyUI also serves by path without prompt_id).
        last = request.app.get("last_prompt_id")
        if last:
            entry = request.app["images"].get(last)
    if not entry:
        return web.Response(status=404, text="not found")
    images = entry["images"]
    idx = 0
    if filename:
        for i, (fn, _) in enumerate(images):
            if fn == filename:
                idx = i
                break
        else:
            idx = min(int(q.get("index", "0") or 0), len(images) - 1)
    _, data = images[idx]
    if data.startswith("data:"):
        data = data.split(",", 1)[1]
    raw = base64.b64decode(data)
    resp = web.Response(body=raw, content_type="image/png")
    resp.headers["Content-Disposition"] = f'inline; filename="{filename or images[idx][0]}"'
    return resp


# ---------------------------------------------------------------- websocket


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    client_id = request.rel_url.query.get("clientId", "unknown")

    class Client:
        client_id: str
        ws: web.WebSocketResponse

    client = Client()
    client.client_id = client_id
    client.ws = ws
    request.app["ws_clients"].append(client)
    log.info("ws connect: clientId=%s", client_id)
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.ERROR:
                log.warning("ws error: %s", ws.exception())
    finally:
        if client in request.app["ws_clients"]:
            request.app["ws_clients"].remove(client)
        log.info("ws disconnect: clientId=%s", client_id)
    return ws


async def handle_upload_image(request: web.Request) -> web.Response:
    """ComfyUI-compatible image upload (Open WebUI's edit flow uses this).

    Stores the data URI in memory under its filename; execute_job() forwards
    it to RunPod as input.images so LoadImage nodes can reference it.
    """
    reader = await request.multipart()
    filename, data_uri, raw = None, None, b""
    async for part in reader:
        if part.name == "image":
            filename = part.filename or "upload.png"
            mime = part.headers.get("Content-Type", "image/png")
            raw = await part.read(decode=False)
            data_uri = f"data:{mime};base64," + base64.b64encode(raw).decode()
            break
        elif part.name == "type":
            await part.read(decode=False)  # 'input' — ignored, always 'input'
    if not filename or not data_uri:
        return web.json_response({"error": "missing image part"}, status=400)

    request.app["uploads"][filename] = data_uri
    # prune
    if len(request.app["uploads"]) > MAX_STORED_UPLOADS:
        for k in list(request.app["uploads"].keys())[: len(request.app["uploads"]) - MAX_STORED_UPLOADS]:
            request.app["uploads"].pop(k, None)
    log.info("uploaded image %s (%d bytes)", filename, len(raw))
    return web.json_response({"name": filename, "subfolder": "", "type": "input"})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "endpoint": ENDPOINT_ID or "(unset)"})


# ---------------------------------------------------------------- app setup


def create_app() -> web.Application:
    app = web.Application(client_max_size=1024 * 1024 * 128)
    app["images"] = {}
    app["errors"] = {}
    app["ws_clients"] = []
    app["last_prompt_id"] = None
    app["uploads"] = {}

    app.router.add_get("/object_info", handle_object_info)
    app.router.add_post("/prompt", handle_prompt)
    app.router.add_get("/history/{prompt_id}", handle_history)
    app.router.add_get("/view", handle_view)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/api/upload/image", handle_upload_image)
    app.router.add_get("/health", handle_health)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8190"))
    log.info("bridge starting, endpoint=%s", ENDPOINT_ID or "(unset)")
    web.run_app(create_app(), host="0.0.0.0", port=port, print=None)