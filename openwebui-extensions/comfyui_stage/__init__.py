"""Stage Open WebUI chat images in ComfyUI's input directory.

The ComfyUI MCP tools take paths inside the ComfyUI host, not chat file IDs.
This tool resolves the user's attachment with Open WebUI's own ownership
checks and uploads the bytes through ComfyUI's documented /api/upload/image
endpoint. No shared mounts, no model-supplied paths, no database writes.
"""

import io
import json
import os
import uuid

import aiohttp
from PIL import Image
from pydantic import BaseModel, Field

from chat_image_retrieval import MAX_PIXELS, owned_context, load_source

_FORMATS = {
    "PNG": ("image/png", "png"),
    # iPhone photos are commonly MPO: a JPEG codestream with multi-picture
    # metadata, which Pillow reports as "MPO". ComfyUI reads them as JPEG.
    "JPEG": ("image/jpeg", "jpg"),
    "MPO": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
    "GIF": ("image/gif", "gif"),
}


def _image_info(raw: bytes) -> tuple[str, str]:
    """Validate the bytes and return (content type, extension)."""
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
            image.verify()
    except Exception:
        raise ValueError("Unsupported or corrupt image.") from None
    if image_format not in _FORMATS:
        raise ValueError("Only PNG, JPEG (including iPhone MPO), WebP, and GIF images are supported.")
    if width * height > MAX_PIXELS:
        raise ValueError("Image exceeds the pixel limit.")
    return _FORMATS[image_format]


def _safe_relative(value: object) -> bool:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        return False
    if "\x00" in value or any(ord(char) < 32 for char in value):
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def _container_path(name: object, subfolder: object = "") -> str:
    """Build the MCP-visible path from ComfyUI's upload response."""
    if not _safe_relative(name) or (subfolder and not _safe_relative(subfolder)):
        raise ValueError("ComfyUI returned an unsafe filename.")
    relative = f"{subfolder}/{name}" if subfolder else name
    return f"/comfyui/input/{relative}"


def _select(latest: list[str], catalog: dict, image_ref: str) -> str | None:
    """Resolve an optional 1-based index or saved-image reference."""
    ref = (image_ref or "").strip()
    if not ref:
        return latest[-1] if latest else None
    if ref.isdigit():
        index = int(ref)
        return latest[index - 1] if 1 <= index <= len(latest) else None
    entry = catalog.get(ref)
    return entry.get("url") if isinstance(entry, dict) else None


async def _upload(raw: bytes, filename: str, content_type: str, base_url: str,
                  timeout: int) -> str:
    """Upload one image through ComfyUI's documented input endpoint."""
    form = aiohttp.FormData()
    form.add_field("image", raw, filename=filename, content_type=content_type)
    form.add_field("type", "input")
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.post(f"{base_url}/api/upload/image", data=form) as response:
            body = await response.read()
            if response.status != 200:
                raise RuntimeError(f"ComfyUI upload failed with HTTP {response.status}.")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        raise RuntimeError("ComfyUI upload returned an invalid response.") from None
    if not isinstance(payload, dict):
        raise RuntimeError("ComfyUI upload returned an invalid response.")
    return _container_path(payload.get("name"), payload.get("subfolder") or "")


class Tools:
    class Valves(BaseModel):
        COMFYUI_URL: str = Field(
            default=os.environ.get("COMFYUI_BASE_URL", ""),
            description=(
                "ComfyUI proxy base URL reachable from Open WebUI "
                "(for example the COMFYUI_BASE_URL value in .env)."
            ),
        )
        TIMEOUT: int = Field(
            default=120, ge=1, le=600, description="Upload timeout in seconds."
        )

    def __init__(self):
        self.valves = self.Valves()

    async def stage_chat_image(
        self,
        image_ref: str = "",
        __user__: dict = None,
        __metadata__: dict = None,
    ) -> str:
        """Copy an image attached to this chat into ComfyUI's input directory.

        Use this before comfyui_generate_video_from_image or comfyui_edit_image
        when the source image was uploaded in the chat. The returned path is
        what those tools accept. The path belongs to the ComfyUI host; do not
        add, remove, or rewrite its components.

        :param image_ref: Optional 1-based index or exact saved-image reference
            such as `<message_id>:<index>` when the chat has more than one
            image. Omit for the most recent uploaded image.
        :return: The ComfyUI input path, or a clear error message.
        """
        try:
            catalog, latest = await owned_context(__user__, __metadata__)
        except Exception:
            return "Image staging requires a saved chat with an uploaded image."
        if not latest:
            return "No uploaded image found in this chat. Attach an image and try again."
        source = _select(latest, catalog, image_ref)
        if source is None:
            return (
                "Invalid image reference. Use a 1-based index from this chat "
                "or an exact saved-image reference."
            )
        try:
            raw = await load_source(source, (__user__ or {}).get("id"))
            content_type, extension = _image_info(raw)
        except ValueError as exc:
            return f"Image staging failed: {exc}"
        except Exception:
            return "Image staging failed: the attachment could not be read."

        base_url = (self.valves.COMFYUI_URL or "").rstrip("/")
        if not base_url:
            return "Image staging failed: COMFYUI_BASE_URL is not configured for Open WebUI."

        filename = f"{uuid.uuid4().hex}.{extension}"
        try:
            path = await _upload(
                raw, filename, content_type, base_url, self.valves.TIMEOUT
            )
        except Exception:
            return (
                "Image staging failed: ComfyUI did not accept the upload. "
                "Check the proxy URL and that ComfyUI is reachable."
            )
        return (
            f"Staged image for ComfyUI:\n- {path}\n"
            "Use this exact path as image_path for comfyui_generate_video_from_image "
            "or comfyui_edit_image. Do not add or remove path components."
        )
