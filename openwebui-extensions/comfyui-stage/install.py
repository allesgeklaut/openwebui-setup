"""Register/update through native OWUI tool APIs; run inside open-webui."""
import asyncio
from pathlib import Path


async def main():
    from open_webui.models.tools import ToolForm, ToolMeta, Tools
    from open_webui.models.users import Users
    from open_webui.utils.plugin import load_tool_module_by_id, replace_imports
    from open_webui.utils.tools import get_tool_specs

    tool_id = "comfyui_stage"
    content = replace_imports(Path(__file__).with_name("tool.py").read_text())
    instance, frontmatter, module = await load_tool_module_by_id(tool_id, content)
    specs = get_tool_specs(instance)
    meta = ToolMeta(
        description=frontmatter.get("description"),
        manifest=frontmatter,
    )
    existing = await Tools.get_tool_by_id(tool_id)
    if existing:
        await Tools.update_tool_by_id(
            tool_id,
            {
                "content": content,
                "name": frontmatter["title"],
                "specs": specs,
                "meta": meta.model_dump(),
                "commit_message": "Load repo-managed ComfyUI image-staging tool",
            },
            user_id=existing.user_id,
            module=instance,
        )
        print(f"{tool_id} updated (owner {existing.user_id}).")
    else:
        users = await Users.get_users(limit=10)
        owner = next(user for user in users["users"] if user.role == "admin")
        await Tools.insert_new_tool(
            owner.id,
            ToolForm(
                id=tool_id,
                name=frontmatter["title"],
                content=content,
                meta=meta,
                commit_message="Register repo-managed ComfyUI image-staging tool",
            ),
            specs,
            module=instance,
        )
        print(f"{tool_id} registered (owner {owner.id}).")
    print("Restart open-webui to refresh imported-module and tool caches.")


asyncio.run(main())
