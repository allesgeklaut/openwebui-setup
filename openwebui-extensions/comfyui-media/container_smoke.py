"""Run inside open-webui; real local APIs/storage, no provider inference.

docker exec -w /app/backend open-webui python \
  /app/backend/extensions/comfyui-media/container_smoke.py
Keeps one private synthetic chat for browser verification; --cleanup removes it.
"""
import asyncio
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock, patch

import comfyui_media as media

STATE = Path("/tmp/comfyui-media-smoke.json")


async def main():
    from open_webui.main import app
    from open_webui.models.chats import Chats, ChatForm
    from open_webui.models.files import Files
    from open_webui.models.users import Users
    from open_webui.storage.provider import Storage
    from open_webui import tasks

    if "--cleanup" in sys.argv:
        state = json.loads(STATE.read_text())
        for file_id in state["files"]:
            record = await Files.get_file_by_id(file_id)
            if record:
                await asyncio.to_thread(Storage.delete_file, record.path)
                await Files.delete_file_by_id(file_id)
        await Chats.delete_chat_by_id(state["chat_id"])
        STATE.unlink()
        print("Synthetic chat and uploaded copies removed.")
        return
    if STATE.exists():
        raise RuntimeError("Clean up the previous synthetic chat first.")
    users = await Users.get_users(limit=10)
    user = next(u for u in users["users"] if u.role == "admin")
    chat_id, message_id, parent_id = [str(uuid.uuid4()) for _ in range(3)]
    filename = "ltx25_mcp_b80fde435f_1024x1024_49f_00001_.mp4"
    tool_output = [
        {"type": "function_call", "call_id": "smoke-call", "name": "comfyui_generate_video",
         "arguments": "{}", "status": "completed"},
        {"type": "function_call_output", "call_id": "smoke-call", "status": "completed",
         "output": [{"type": "input_text", "text": "- video: " + media.HOST_ROOT + filename}]},
    ]
    now = int(time.time())
    parent = {"id": parent_id, "parentId": None, "childrenIds": [message_id],
              "role": "user", "content": "Local video attachment verification (no model call).", "timestamp": now}
    message = {"id": message_id, "parentId": parent_id, "childrenIds": [], "role": "assistant",
               "content": "Locally generated clip — automatic chat attachment test.",
               "output": [], "done": True, "timestamp": now, "model": "local-smoke",
               "modelName": "Local smoke test", "modelIdx": 0}
    record = await Chats.insert_new_chat(chat_id, user.id, ChatForm(chat={
        "title": "ComfyUI attachment verification", "models": ["local-smoke"],
        "history": {"currentId": message_id, "messages": {parent_id: parent, message_id: message}},
        "messages": [parent, message], "timestamp": now,
    }))
    assert record
    STATE.write_text(json.dumps({"chat_id": chat_id, "files": []}))
    request = SimpleNamespace(app=app, state=SimpleNamespace())
    # main.app has no lifespan in this test process; tasks use local buffers.
    app.state.redis = None
    metadata = {"chat_id": chat_id, "message_id": message_id}
    owner = {"id": user.id}
    emitter = AsyncMock()
    function = media.Filter()
    body = {"messages": [{"role": "user", "content": "unchanged poster context"}]}
    with patch.object(tasks, "item_tasks", {chat_id: ["media-smoke"]}), \
         patch.object(tasks, "response_streams", {}):
        await tasks.save_response_stream(None, "media-smoke", chat_id, message_id, tool_output)
        assert media.video_sources((await media.owned_response(owner, metadata, request))["output"]) == [filename]
        # Another response's stream must not authorize a file.
        tasks.response_streams["media-smoke"]["message_id"] = "other-message"
        assert media.video_sources((await media.owned_response(owner, metadata, request))["output"]) == []
        tasks.response_streams["media-smoke"]["message_id"] = message_id
        try:
            await media.owned_response({"id": str(uuid.uuid4())}, metadata, request)
        except ValueError:
            pass
        else:
            raise AssertionError("Foreign user accessed private chat")
        result = await function.request(body, owner, metadata, request, emitter)
        assert len(result["messages"]) == 2, "Real upload or delivery guidance failed"
        saved = await media.owned_response(owner, metadata, request)
        attachments = saved["files"]
        STATE.write_text(json.dumps({"chat_id": chat_id, "files": [a["id"] for a in attachments]}))
        assert len(attachments) == 1
        attachment = attachments[0]
        file = await Files.get_file_by_id_and_user_id(attachment["id"], user.id)
        assert file and file.meta["content_type"] == "video/mp4"
        assert Path(Storage.get_file(file.path)).read_bytes() == media.read_video(filename)
        emitter.assert_awaited_once()
        assert emitter.await_args.args[0]["type"] == "chat:message:files"
        # Fresh request simulates resume; no duplicate native upload.
        fresh = SimpleNamespace(app=app, state=SimpleNamespace())
        with patch.object(media, "upload_video", AsyncMock(side_effect=AssertionError("duplicate upload"))):
            repeated = await function.request(body, owner, metadata, fresh, emitter)
            assert repeated == result
        message.update(output=tool_output, files=attachments)
        outlet = await function.outlet({"messages": [message]}, owner, metadata, request, emitter)
        rendered = outlet["messages"][0]
        token = "<video>\n" + attachment["url"] + "\n</video>"
        assert token in rendered["content"]
        assert any(part.get("text") == token for item in rendered["output"]
                   for part in item.get("content", []))
        assert await function.outlet(outlet, owner, metadata, request, emitter) == outlet
        await Chats.upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, rendered)
    persisted = await Chats.get_chat_by_id_and_user_id(chat_id, user.id)
    assert token in persisted.chat["history"]["messages"][message_id]["content"]
    # Exercise the registered native pipeline, including injected hook arguments.
    from open_webui.models.functions import Functions
    from open_webui.utils.filter import process_filter_functions
    registered = await Functions.get_function_by_id("comfyui_media")
    assert registered and registered.is_active and registered.is_global
    params = {"__user__": user.model_dump(), "__metadata__": metadata,
              "__request__": request, "__event_emitter__": emitter}
    pipeline_body, _ = await process_filter_functions(
        request, None, [registered], "request", body, params)
    assert len(pipeline_body["messages"]) == 2
    pipeline_outlet, _ = await process_filter_functions(
        request, None, [registered], "outlet", outlet, params)
    assert pipeline_outlet == outlet
    print(json.dumps({"status": "pass", "provider_calls": 0, "native_upload": "pass",
                      "ownership": "pass", "live_stream_provenance": "pass",
                      "persistence_events_resume_idempotence": "pass", "player_output": "pass",
                      "registered_pipeline": "pass", "chat_id": chat_id, "file_id": file.id}))


asyncio.run(main())
