import io
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

import comfyui_stage as stage

USER = {"id": "owner"}
META = {"chat_id": "chat", "message_id": "answer"}


def image_bytes(fmt="PNG", size=(4, 4), color="red"):
    buffer = io.BytesIO()
    if fmt == "MPO":
        Image.new("RGB", size, color).save(
            buffer, format="MPO", save_all=True,
            append_images=[Image.new("RGB", size, "blue")])
    else:
        Image.new("RGB", size, color).save(buffer, format=fmt)
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, body=b"", status=200):
        self.body = body
        self.status = status

    async def read(self):
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, data):
        self.calls.append((url, data))
        return self.response


class ImageInfoTests(unittest.TestCase):
    def test_supported_formats(self):
        for fmt, expected in [("PNG", ("image/png", "png")),
                              ("JPEG", ("image/jpeg", "jpg")),
                              ("MPO", ("image/jpeg", "jpg")),
                              ("WEBP", ("image/webp", "webp")),
                              ("GIF", ("image/gif", "gif"))]:
            self.assertEqual(stage._image_info(image_bytes(fmt)), expected)

    def test_unsupported_format_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Only PNG, JPEG"):
            stage._image_info(image_bytes("BMP"))

    def test_corrupt_bytes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported or corrupt"):
            stage._image_info(b"not an image")

    def test_pixel_limit_is_enforced(self):
        raw = image_bytes(size=(4, 4))
        with patch.object(stage, "MAX_PIXELS", 15):
            with self.assertRaisesRegex(ValueError, "pixel limit"):
                stage._image_info(raw)


class ContainerPathTests(unittest.TestCase):
    def test_input_paths(self):
        self.assertEqual(stage._container_path("abc.png"), "/comfyui/input/abc.png")
        self.assertEqual(stage._container_path("abc.png", "sub"),
                         "/comfyui/input/sub/abc.png")

    def test_unsafe_names_are_rejected(self):
        for name in ["", None, "/abs.png", "../x.png", "a/../b.png", "a//b.png",
                     "a\\b.png", "a\x00b.png", "a\nb.png", ".", "..", 7]:
            with self.assertRaises(ValueError):
                stage._container_path(name)

    def test_unsafe_subfolder_is_rejected(self):
        for subfolder in ["/abs", "../x", "a\\b", "a//b"]:
            with self.assertRaises(ValueError):
                stage._container_path("abc.png", subfolder)


class SelectionTests(unittest.TestCase):
    def test_latest_image_is_default(self):
        self.assertEqual(stage._select(["one.png", "two.png"], {}, ""), "two.png")
        self.assertEqual(stage._select(["one.png", "two.png"], {}, "  "), "two.png")

    def test_one_based_index(self):
        latest = ["one.png", "two.png"]
        self.assertEqual(stage._select(latest, {}, "1"), "one.png")
        self.assertEqual(stage._select(latest, {}, "2"), "two.png")
        for ref in ["0", "3", "-1", "1.0"]:
            self.assertIsNone(stage._select(latest, {}, ref))

    def test_saved_image_reference(self):
        catalog = {"msg:1": {"url": "/api/v1/files/abc/content"}}
        self.assertEqual(stage._select([], catalog, "msg:1"),
                         "/api/v1/files/abc/content")
        self.assertIsNone(stage._select([], catalog, "msg:2"))
        self.assertIsNone(stage._select([], {"msg:1": "wrong"}, "msg:1"))

    def test_no_images(self):
        self.assertIsNone(stage._select([], {}, ""))


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def upload(self, response):
        session = FakeSession(response)
        with patch.object(stage.aiohttp, "ClientSession", lambda **kwargs: session):
            result = await stage._upload(
                b"bytes", "abc.png", "image/png", "http://comfy", 5)
        return result, session

    async def test_upload_returns_mcp_path(self):
        result, session = await self.upload(FakeResponse(
            json.dumps({"name": "abc.png", "subfolder": "", "type": "input"}).encode()))
        self.assertEqual(result, "/comfyui/input/abc.png")
        self.assertEqual(session.calls[0][0], "http://comfy/api/upload/image")

    async def test_subfolder_is_preserved(self):
        result, _ = await self.upload(FakeResponse(
            json.dumps({"name": "abc.png", "subfolder": "mcp"}).encode()))
        self.assertEqual(result, "/comfyui/input/mcp/abc.png")

    async def test_http_error_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            await self.upload(FakeResponse(b"", status=500))

    async def test_invalid_response_is_reported(self):
        for body in [b"not json", b"[]", b"\xff"]:
            with self.assertRaises(RuntimeError):
                await self.upload(FakeResponse(body))

    async def test_unsafe_response_name_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.upload(FakeResponse(json.dumps({"name": "../x.png"}).encode()))


class StageToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tool = stage.Tools()
        self.tool.valves.COMFYUI_URL = "http://comfy"

    async def stage(self, catalog=None, latest=None, image_ref="", **patches):
        defaults = {
            "owned_context": AsyncMock(return_value=(catalog or {}, latest or [])),
            "load_source": AsyncMock(return_value=image_bytes()),
        }
        defaults.update(patches)
        stack = [patch.object(stage, name, value) for name, value in defaults.items()]
        for patcher in stack:
            patcher.start()
            self.addCleanup(patcher.stop)
        return await self.tool.stage_chat_image(
            image_ref, __user__=USER, __metadata__=META)

    async def test_unowned_or_unsaved_chat_is_rejected(self):
        result = await self.stage(
            owned_context=AsyncMock(side_effect=ValueError("not owned")))
        self.assertIn("saved chat", result)

    async def test_chat_without_images_is_reported(self):
        result = await self.stage()
        self.assertIn("No uploaded image found", result)

    async def test_invalid_reference_is_reported(self):
        result = await self.stage(latest=["/api/v1/files/a/content"], image_ref="9")
        self.assertIn("Invalid image reference", result)

    async def test_unsupported_attachment_is_reported(self):
        result = await self.stage(
            latest=["/api/v1/files/a/content"],
            load_source=AsyncMock(return_value=image_bytes("BMP")))
        self.assertIn("Only PNG, JPEG", result)

    async def test_unreadable_attachment_is_reported(self):
        result = await self.stage(
            latest=["/api/v1/files/a/content"],
            load_source=AsyncMock(side_effect=ValueError("Image attachment is unavailable.")))
        self.assertIn("Image attachment is unavailable.", result)

    async def test_missing_comfyui_url_is_reported(self):
        self.tool.valves.COMFYUI_URL = ""
        result = await self.stage(latest=["/api/v1/files/a/content"])
        self.assertIn("COMFYUI_BASE_URL is not configured", result)

    async def test_successful_staging_returns_path_and_instruction(self):
        raw = image_bytes()
        upload = AsyncMock(return_value="/comfyui/input/abc.png")
        result = await self.stage(
            latest=["/api/v1/files/a/content"], load_source=AsyncMock(return_value=raw),
            _upload=upload)
        self.assertIn("/comfyui/input/abc.png", result)
        self.assertIn("image_path", result)
        args = upload.await_args.args
        self.assertEqual(args[0], raw)
        self.assertRegex(args[1], r"^[0-9a-f]{32}\.png$")
        self.assertEqual(args[2:], ("image/png", "http://comfy", 120))

    async def test_upload_failure_is_reported_without_details(self):
        result = await self.stage(
            latest=["/api/v1/files/a/content"],
            _upload=AsyncMock(side_effect=RuntimeError("boom /secret/path")))
        self.assertIn("ComfyUI did not accept the upload", result)
        self.assertNotIn("/secret/path", result)


class RegistrationEntryTests(unittest.TestCase):
    def test_adapter_imports_the_runtime_package(self):
        code = (Path(__file__).parent / "tool.py").read_text()
        namespace = {}
        exec(compile(code, "tool.py", "exec"), namespace)
        self.assertIs(namespace["Tools"], stage.Tools)


if __name__ == "__main__":
    unittest.main()
