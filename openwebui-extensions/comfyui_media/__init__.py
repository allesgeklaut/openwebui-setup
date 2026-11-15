"""Attach completed ComfyUI MP4 outputs to owned OWUI chats, without HTTP fetches."""

import asyncio
import copy
import io
import json
import logging
import os
from pathlib import Path
import re

from chat_image_retrieval import (
    IMAGE_TYPES, fingerprint, image_url, read_local_file,
    response_image_snapshot, response_source_keys,
)

log = logging.getLogger(__name__)
MEDIA_ROOT = os.environ.get("COMFYUI_MEDIA_DIR", "/app/backend/comfyui-media")
HOST_ROOT = "/opt/stacks/comfyui/output/mcp-video/"
MAX_VIDEO_BYTES = 32 * 1024 * 1024
MAX_VIDEOS = 4
STATE_KEY = "comfyui_media_v1"
SOURCE_KEY = "comfyui_video_source"
FILENAME = re.compile(r"ltx25_mcp_[0-9a-f]{10}_[0-9]{3,4}x[0-9]{3,4}_[0-9]{1,3}f_[0-9]+_\.mp4\Z")
TOOL_NAMES = {"comfyui_generate_video", "comfyui_generate_video_from_image", "comfyui_generate_gif"}
SYNTHETIC_ANALYSIS = "Here are the images from the tool results above. Please analyze them."
VIDEO_PREVIEW_CONTEXT = (
    "Video tool result: these images are poster previews of the generated video, "
    "not a new user request for image analysis. Continue the original user request."
)
VIDEO_GUIDANCE = (
    "ComfyUI video workflow: a completed video result already contains the MP4 download "
    "URL and a poster preview. For a generation-only request, deliver the existing "
    "video using its attachment or returned download link. Do not generate replacement "
    "images/videos unless the user asks or confirms a proposed retry. A poster is only "
    "a still; do not claim to have watched the clip or verified its motion/audio from it. "
    "If the user requests video inspection or editing, download the MP4 from the "
    "returned HTTP URL into the terminal, then sample frames for visual inspection "
    "or process it as requested. The advertised /opt/stacks output path belongs to "
    "the ComfyUI host and is not accessible in the terminal container; do not search "
    "the terminal filesystem for it."
)


def video_sources(output):
    """Only completed, paired ComfyUI tool results authorize a local file read."""
    calls = {item.get("call_id") for item in output
             if isinstance(item, dict) and item.get("type") == "function_call"
             and item.get("name") in TOOL_NAMES
             and isinstance(item.get("call_id"), str) and item.get("call_id")}
    sources = []
    for item in output:
        if (not isinstance(item, dict) or item.get("type") != "function_call_output"
                or item.get("status") != "completed" or item.get("call_id") not in calls):
            continue
        for part in item.get("output") or []:
            if not isinstance(part, dict) or part.get("type") != "input_text":
                continue
            for line in (part.get("text") or "").splitlines():
                if not line.startswith("- video: " + HOST_ROOT):
                    continue
                filename = line[len("- video: " + HOST_ROOT):]
                if FILENAME.fullmatch(filename) and filename not in sources:
                    sources.append(filename)
    return sources[:MAX_VIDEOS]


def read_video(filename):
    if not FILENAME.fullmatch(filename):
        raise ValueError("Unsupported ComfyUI output name.")
    raw = read_local_file(Path(MEDIA_ROOT) / filename, MEDIA_ROOT, MAX_VIDEO_BYTES)
    if len(raw) < 12 or raw[4:8] != b"ftyp":
        raise ValueError("Output is not an MP4 file.")
    return raw


async def owned_response(user, metadata, request):
    """Read server-side provenance, never model-supplied messages or URLs."""
    from open_webui.models.chats import Chats
    from open_webui.tasks import get_response_streams_by_chat_id

    user_id = (user or {}).get("id")
    chat_id, message_id = (metadata or {}).get("chat_id"), (metadata or {}).get("message_id")
    if not all(isinstance(value, str) and value for value in (user_id, chat_id, message_id)):
        raise ValueError("A saved owned chat is required.")
    record = await Chats.get_chat_by_id_and_user_id(chat_id, user_id)
    if record is None:
        raise ValueError("A saved owned chat is required.")
    message = (record.chat.get("history") or {}).get("messages", {}).get(message_id)
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ValueError("A saved assistant response is required.")
    message = dict(message)
    streams = await get_response_streams_by_chat_id(request.app.state.redis, chat_id)
    for stream in streams:
        if stream.get("chat_id") == chat_id and stream.get("message_id") == message_id:
            message["output"] = stream.get("output") or []
    return message


async def upload_video(request, user, metadata, filename, raw):
    from starlette.datastructures import Headers, UploadFile
    from open_webui.models.users import Users
    from open_webui.routers.files import upload_file_handler

    owner = await Users.get_user_by_id(user["id"])
    if owner is None:
        raise ValueError("User is unavailable.")
    upload = UploadFile(
        file=io.BytesIO(raw), filename=filename,
        headers=Headers({"content-type": "video/mp4"}), size=len(raw))
    try:
        record = await upload_file_handler(
            request, file=upload, user=owner, process=False,
            process_in_background=False, metadata={
                "chat_id": metadata["chat_id"], "message_id": metadata["message_id"],
                SOURCE_KEY: filename})
    finally:
        await upload.close()
    file_id = record["id"] if isinstance(record, dict) else record.id
    return {
        "id": file_id, "type": "file", "name": filename,
        "url": f"/api/v1/files/{file_id}/content",
        "content_type": "video/mp4", "size": len(raw), SOURCE_KEY: filename,
    }


async def persist_video(request, user, metadata, attachment):
    from open_webui.models.chats import Chats

    # Recheck access after the upload/read awaits.
    await owned_response(user, metadata, request)
    files = await Chats.add_message_files_by_id_and_message_id(
        metadata["chat_id"], metadata["message_id"], [attachment])
    if files is None:
        raise ValueError("Chat is unavailable.")
    return files


async def video_guidance(body, message, user):
    """Neutralize only synthetic prompts whose images are verified video posters.

    A genuine user instruction, a mixed image batch, or another tool's images
    must keep its original wording. No upstream middleware patch is required.
    """
    output = message.get("output") or []
    calls = [item for item in output if isinstance(item, dict)
             and item.get("type") == "function_call"
             and item.get("name") in TOOL_NAMES - {"comfyui_generate_gif"}
             and isinstance(item.get("call_id"), str) and item["call_id"]]
    results = [item for item in output if isinstance(item, dict)
               and item.get("type") == "function_call_output"
               and any(item.get("call_id") == call["call_id"]
                       and video_sources([call, item]) for call in calls)]
    if not results:
        return body
    keys = await response_source_keys(
        response_image_snapshot({"output": results}), user["id"])
    result = dict(body)
    result["messages"] = copy.deepcopy(body.get("messages") or [])
    result_ids = {item["call_id"] for item in results}
    pending_video_result = False
    for current in result["messages"]:
        if current.get("role") == "tool" and current.get("tool_call_id") in result_ids:
            pending_video_result = True
        if current.get("role") != "user":
            continue
        follows_video_result = pending_video_result
        pending_video_result = False
        content = current.get("content")
        if not follows_video_result or not isinstance(content, list):
            continue
        text = [part for part in content if isinstance(part, dict)
                and part.get("type") in {"text", "input_text"}]
        images = [part for part in content if isinstance(part, dict)
                  and part.get("type") in IMAGE_TYPES]
        if (len(text) != 1 or text[0].get("text") != SYNTHETIC_ANALYSIS
                or not images or len(text) + len(images) != len(content)):
            continue
        identities = await asyncio.to_thread(
            lambda: [fingerprint(image_url(part)) for part in images])
        if all(identity is not None and identity in keys for identity in identities):
            text[0]["text"] = VIDEO_PREVIEW_CONTEXT
    append_system_notice(result["messages"], VIDEO_GUIDANCE)
    return result


def append_system_notice(messages, text):
    # OWUI's Responses converter retains only the last system message.
    # Extend it instead of adding one that would discard existing instructions.
    system = next((item for item in reversed(messages) if item.get("role") == "system"), None)
    if system is None:
        messages.append({"role": "system", "content": text})
    elif isinstance(system.get("content"), list):
        if not any(part.get("text") == text for part in system["content"] if isinstance(part, dict)):
            system["content"].append({"type": "text", "text": text})
    elif text not in (system.get("content") or ""):
        system["content"] = (system.get("content") or "") + "\n\n" + text


def displayed_video_urls(output):
    """Recognize successful native displays from owned server-side tool output."""
    calls = {item.get("call_id") for item in output
             if isinstance(item, dict) and item.get("type") == "function_call"
             and item.get("name") == "display_file"
             and isinstance(item.get("call_id"), str) and item["call_id"]}
    urls = set()
    for item in output:
        if (not isinstance(item, dict) or item.get("type") != "function_call_output"
                or item.get("status") != "completed" or item.get("call_id") not in calls):
            continue
        for part in item.get("output") or []:
            if not isinstance(part, dict) or part.get("type") != "input_text":
                continue
            try:
                result = json.loads(part.get("text", ""))
            except (ValueError, TypeError):
                continue
            if (isinstance(result, dict) and result.get("displayed") is True
                    and result.get("source") == "open_webui"
                    and result.get("content_type") == "video/mp4"
                    and isinstance(result.get("path"), str)):
                urls.add(result["path"])
    return urls


def attachment_notice(body, attachments, displayed=()):
    """Inform the model without altering image parts or injecting MP4 bytes."""
    result = dict(body)
    result["messages"] = copy.deepcopy(body.get("messages") or [])
    if attachments:
        pending = [item for item in attachments if item["url"] not in displayed]
        notice = (
            "ComfyUI video delivery: the generated MP4 is already saved as an owned "
            "attachment in this chat. No HTTP download or filesystem search is needed "
            "to deliver it. Use the attachment link below; downloading is only needed "
            "if you want to process the actual video. The poster is a still, not the "
            "complete video. Use OWUI's native display_file tool exactly once per "
            "attachment listed as not yet displayed below, passing its /api/v1/files/ "
            "content URL as path. After a successful display_file result, do not "
            "display it again or add video embed markup. This filter does not add "
            "another player. If the native tool is unavailable or display fails, "
            "provide the download link instead; do not repeatedly retry or generate "
            "a replacement:\n"
            + "\n".join(f"- {item['url']}" for item in attachments))
        notice += "\nNot yet displayed:\n" + (
            "\n".join(f"- {item['url']}" for item in pending)
            if pending else "None. All of these videos were already displayed; just confirm completion.")
        append_system_notice(result["messages"], notice)
    return result


try:
    from pydantic import BaseModel

    class Valves(BaseModel):
        priority: int = 10
except ImportError:
    class Valves:
        priority = 10


class Filter:
    Valves = Valves

    def __init__(self):
        self.valves = self.Valves()

    async def _attachments(self, user, metadata, request, emitter):
        if request is None:
            return []
        binding = ((user or {}).get("id"), (metadata or {}).get("chat_id"),
                   (metadata or {}).get("message_id"))
        states = getattr(request.state, STATE_KEY, None)
        if states is None:
            states = {}
            setattr(request.state, STATE_KEY, states)
        state = states.setdefault(binding, {"lock": asyncio.Lock(), "uploads": {}})
        async with state["lock"]:
            message = await owned_response(user, metadata, request)
            attachments = [item for item in message.get("files") or []
                           if isinstance(item, dict) and item.get(SOURCE_KEY)]
            by_source = {item[SOURCE_KEY]: item for item in attachments}
            for filename in video_sources(message.get("output") or []):
                if filename in by_source:
                    continue
                raw = await asyncio.to_thread(read_video, filename)
                # Cache upload before persistence, so a retry does not upload twice.
                attachment = state["uploads"].get(filename)
                if attachment is None:
                    attachment = await upload_video(request, user, metadata, filename, raw)
                    state["uploads"][filename] = attachment
                files = await persist_video(request, user, metadata, attachment)
                attachments.append(attachment)
                by_source[filename] = attachment
                if emitter:
                    await emitter({"type": "chat:message:files", "data": {"files": files}})
            return attachments

    async def request(self, body: dict, __user__=None, __metadata__=None,
                      __request__=None, __event_emitter__=None):
        result = body
        displayed = set()
        try:
            if __request__ is not None:
                message = await owned_response(__user__, __metadata__, __request__)
                result = await video_guidance(body, message, __user__)
                displayed = displayed_video_urls(message.get("output") or [])
        except Exception as exc:
            log.warning("ComfyUI video guidance unavailable (%s)", type(exc).__name__)
        try:
            attachments = await self._attachments(
                __user__, __metadata__, __request__, __event_emitter__)
            return attachment_notice(result, attachments, displayed)
        except Exception as exc:
            # Optional delivery must never invalidate an otherwise successful
            # generation. Its original MP4 URL and poster remain usable.
            log.warning("ComfyUI video attachment unavailable (%s); original link retained",
                        type(exc).__name__)
            return result

    async def outlet(self, body: dict, __user__=None, __metadata__=None,
                      __request__=None, __event_emitter__=None):
        try:
            # Persist a late result if no continuation request ran, but leave
            # presentation entirely to the native display tool/download link.
            await self._attachments(
                __user__, __metadata__, __request__, __event_emitter__)
        except Exception as exc:
            log.warning("ComfyUI video attachment unavailable (%s); original link retained",
                        type(exc).__name__)
        return body
