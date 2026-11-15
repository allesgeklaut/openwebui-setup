"""Installed-runtime guidance regression; temporary files/mocked DB reads only.

docker exec -w /app/backend open-webui python \
  /app/backend/extensions/comfyui-media/guidance_smoke.py
No upload, chat writes, provider calls, or GPU work.
"""
import asyncio
import base64
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image
import comfyui_media as media


async def main():
    from open_webui import config, tasks
    from open_webui.models.chats import Chats
    from open_webui.models.files import Files
    from open_webui.routers.openai import convert_to_responses_payload
    from open_webui.utils.middleware import (
        convert_url_images_to_base64, process_messages_with_output,
    )

    with tempfile.TemporaryDirectory(prefix="comfyui-guidance-") as folder:
        path = Path(folder) / "poster.png"
        Image.new("RGB", (32, 18), "blue").save(path)
        encoded = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
        file_id = "00000000-0000-4000-8000-000000000001"
        url = f"/api/v1/files/{file_id}/content"
        filename = "ltx25_mcp_0123456789_1024x576_73f_00001_.mp4"
        attachment = {"id": "00000000-0000-4000-8000-000000000002",
                      "url": "/api/v1/files/00000000-0000-4000-8000-000000000002/content",
                      media.SOURCE_KEY: filename}
        output = [
            {"type": "function_call", "call_id": "video-call",
             "name": "comfyui_generate_video", "arguments": "{}", "status": "completed"},
            {"type": "function_call_output", "call_id": "video-call", "status": "completed",
             "output": [{"type": "input_text", "text": "- video: " + media.HOST_ROOT + filename},
                        {"type": "input_image", "image_url": url}]},
        ]
        original = {"role": "user", "content": "Generate a landscape 3 second video."}
        # Let the installed OWUI serializer create its actual synthetic prompt.
        messages = process_messages_with_output([
            {"role": "system", "content": "Original system policy must survive."},
            original, {"role": "assistant", "output": output}])
        assert media.SYNTHETIC_ANALYSIS in json.dumps(messages)
        saved = SimpleNamespace(chat={"history": {"messages": {
            "answer": {"role": "assistant", "output": [], "files": [attachment]}}}})
        request = SimpleNamespace(state=SimpleNamespace(),
                                  app=SimpleNamespace(state=SimpleNamespace(redis=None)))
        metadata = {"chat_id": "synthetic-guidance-chat", "message_id": "answer"}
        user = {"id": "owner"}
        record = SimpleNamespace(id=file_id, user_id="owner", path=str(path),
                                 meta={"content_type": "image/png"})
        with patch.object(Chats, "get_chat_by_id_and_user_id", AsyncMock(return_value=saved)), \
             patch.object(Files, "get_file_by_id", AsyncMock(return_value=record)), \
             patch.object(Files, "get_file_by_id_and_user_id", AsyncMock(return_value=record)), \
             patch.object(config, "UPLOAD_DIR", Path(folder)), \
             patch.object(tasks, "item_tasks", {metadata["chat_id"]: ["guidance-task"]}), \
             patch.object(tasks, "response_streams", {}), \
             patch.object(media, "upload_video", AsyncMock(side_effect=AssertionError("Unexpected upload"))):
            await tasks.save_response_stream(
                None, "guidance-task", metadata["chat_id"], metadata["message_id"], output)
            body = await convert_url_images_to_base64(
                {"model": "synthetic-vision-model", "messages": messages},
                user=SimpleNamespace(id="owner", role="user"))
            before = copy.deepcopy(body)
            function = media.Filter()
            # Reproduce the old behavior in an isolated mock, without changing code.
            with patch.object(media, "video_guidance", AsyncMock(side_effect=lambda b, *_: b)):
                uncorrected = await function.request(body, user, metadata, request)
                assert media.SYNTHETIC_ANALYSIS in json.dumps(uncorrected)
            corrected = await function.request(body, user, metadata, request)
            payload = convert_to_responses_payload(copy.deepcopy(corrected))
            serialized = json.dumps(payload)
            assert media.SYNTHETIC_ANALYSIS not in serialized
            assert media.VIDEO_PREVIEW_CONTEXT in serialized
            assert media.VIDEO_GUIDANCE in serialized
            assert "Original system policy must survive." in payload["instructions"]
            assert encoded in serialized, "Poster was lost or altered"
            assert original in corrected["messages"], "Actual user request was changed"
            assert body == before
            assert attachment["url"] in serialized
            # Reproduce a model calling the native display tool after upload.
            display = [
                {"type": "function_call", "call_id": "display-call", "name": "display_file"},
                {"type": "function_call_output", "call_id": "display-call", "status": "completed",
                 "output": [{"type": "input_text", "text": json.dumps({
                     "source": "open_webui", "displayed": True,
                     "content_type": "video/mp4", "path": attachment["url"]})}]},
            ]
            await tasks.save_response_stream(
                None, "guidance-task", metadata["chat_id"], metadata["message_id"], output + display)
            outlet_body = {"messages": [{"id": "answer", "role": "assistant",
                                        "content": "Video ready.", "output": output + display}]}
            outlet_result = await function.outlet(outlet_body, user, metadata, request)
            # Presentation is the native display tool's alone; outlet never adds markup.
            assert outlet_result is outlet_body
            assert "<video>" not in outlet_result["messages"][0]["content"]
            second = await function.request(outlet_body, user, metadata, request)
            second_system = str(second["messages"][-1]["content"])
            assert "display_file" in second_system
            assert "Not yet displayed:\nNone." in second_system
            assert attachment["url"] in second_system
    print(json.dumps({"status": "pass", "provider_calls": 0, "db_writes": 0,
                      "native_synthetic_prompt": "reproduced",
                      "owned_live_video_url_to_base64_matching": "pass",
                      "neutral_continuation_and_fetch_guidance": "pass",
                      "poster_original_request_and_responses_payload": "pass",
                      "outlet_never_appends_player": "pass",
                      "notice_marks_displayed_attachment_done": "pass"}))


asyncio.run(main())
