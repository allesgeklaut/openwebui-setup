"""Register/update through native OWUI APIs; run inside open-webui."""
import asyncio
from pathlib import Path


async def main():
    from open_webui.models.functions import Functions, FunctionForm
    from open_webui.models.users import Users
    from open_webui.utils.plugin import load_function_module_by_id

    function_id = "comfyui_media"
    content = Path(__file__).with_name("filter.py").read_text()
    instance, kind, frontmatter, module = await load_function_module_by_id(function_id, content)
    assert kind == "filter"
    existing = await Functions.get_function_by_id(function_id)
    if existing:
        owner_id = existing.user_id
        await Functions.update_function_by_id(
            function_id, {"content": content, "name": frontmatter["title"]},
            user_id=owner_id, module=module)
    else:
        users = await Users.get_users(limit=10)
        owner_id = next(user.id for user in users["users"] if user.role == "admin")
        await Functions.insert_new_function(owner_id, kind, FunctionForm(
            id=function_id, name=frontmatter["title"], content=content,
            meta={"description": frontmatter["description"]}), module=module)
    await Functions.update_function_by_id(
        function_id, {"is_active": True, "is_global": True},
        user_id=owner_id, module=module)
    print("comfyui_media registered, active and global; restart OWUI to refresh caches.")


asyncio.run(main())
