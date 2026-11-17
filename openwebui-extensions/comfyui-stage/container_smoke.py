"""Installed-runtime staging check; synthetic chat, real OWUI storage and upload.

docker exec -i -w /app/backend open-webui \
  python /app/backend/extensions/comfyui-stage/container_smoke.py
No model calls, GPU work, or real user data. Prints the ComfyUI input file it
created; remove that file from /opt/stacks/comfyui/input after the check.
"""
import asyncio
import io
import json
import time
import uuid

from PIL import Image

async def main():
    from open_webui.models.chats import ChatForm, Chats
    from open_webui.models.files import FileForm, Files
    from open_webui.models.users import Users
    from open_webui.storage.provider import Storage
    import comfyui_stage

    users = await Users.get_users(limit=10)
    user = next(candidate for candidate in users["users"] if candidate.role == "admin")
    chat_id = str(uuid.uuid4())
    file_id = str(uuid.uuid4())
    user_message_id = str(uuid.uuid4())
    answer_id = str(uuid.uuid4())
    name = f"stage-smoke-{uuid.uuid4().hex}.png"

    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(buffer, format="PNG")
    _, path = await asyncio.to_thread(
        Storage.upload_file, io.BytesIO(buffer.getvalue()), name, {})
    file = await Files.insert_new_file(user.id, FileForm(
        id=file_id, filename=name, path=path,
        meta={"content_type": "image/png", "size": len(buffer.getvalue())}))
    assert file is not None
    now = int(time.time())
    user_message = {
        "id": user_message_id, "parentId": None, "childrenIds": [answer_id],
        "role": "user", "content": "Synthetic staging check.", "timestamp": now,
        "files": [{"type": "image", "content_type": "image/png", "name": name,
                   "url": f"/api/v1/files/{file_id}/content"}],
    }
    answer = {
        "id": answer_id, "parentId": user_message_id, "childrenIds": [],
        "role": "assistant", "content": "Synthetic answer.", "timestamp": now,
    }
    record = await Chats.insert_new_chat(chat_id, user.id, ChatForm(chat={
        "title": "ComfyUI staging verification", "models": ["local-smoke"],
        "history": {"currentId": answer_id,
                    "messages": {user_message_id: user_message, answer_id: answer}},
        "messages": [user_message, answer], "timestamp": now,
    }))
    assert record is not None

    metadata = {"chat_id": chat_id, "message_id": answer_id}
    tool = comfyui_stage.Tools()
    try:
        foreign = await tool.stage_chat_image(
            __user__={"id": str(uuid.uuid4())}, __metadata__=metadata)
        assert "saved chat" in foreign, foreign
        invalid = await tool.stage_chat_image(
            "9", __user__=user.model_dump(), __metadata__=metadata)
        assert "Invalid image reference" in invalid, invalid
        result = await tool.stage_chat_image(
            __user__=user.model_dump(), __metadata__=metadata)
        marker = "/comfyui/input/"
        assert marker in result, result
        staged = result.split(marker, 1)[1].splitlines()[0].strip()
        assert staged.endswith(".png") and "/" not in staged, staged
        print(json.dumps({
            "status": "pass", "provider_calls": 0, "upload": "pass",
            "ownership": "pass", "staged_path": marker + staged,
        }))
    finally:
        await Chats.delete_chat_by_id(chat_id)
        await asyncio.to_thread(Storage.delete_file, path)
        await Files.delete_file_by_id(file_id)


asyncio.run(main())
