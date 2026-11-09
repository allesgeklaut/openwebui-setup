import asyncio
import ast
import base64
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

import chat_image_retrieval as extension


def png(color="red", size=(8, 8)):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "PNG")
    return output.getvalue()


def gif(frames=2, size=(8, 8), duration=None):
    colors = ("red", "blue", "green", "yellow", "purple", "orange", "cyan", "white")
    images = [Image.new("RGB", size, colors[index % len(colors)]) for index in range(frames)]
    output = io.BytesIO()
    options = {"duration": duration} if duration is not None else {}
    images[0].save(output, "GIF", save_all=True, append_images=images[1:], **options)
    return output.getvalue()


def uri(raw):
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def gif_uri(raw):
    return "data:image/gif;base64," + base64.b64encode(raw).decode()


def part(url):
    return {"type": "image_url", "image_url": {"url": url}}


OLD, NEW = uri(png()), uri(png("blue"))
ANIMATED_GIF, STATIC_GIF = gif_uri(gif()), gif_uri(gif(1))
FILE_URL = "/api/v1/files/upload-test/content"
FILE_ID = "00000000-0000-4000-8000-000000000002"
USER = {"id": "owner"}
META = {"chat_id": "chat", "message_id": "answer"}


def branch():
    return [
        {"id": "first", "parentId": None, "role": "user", "files": [
            {"type": "image", "url": OLD, "name": "untrusted filename"}]},
        {"id": "second", "parentId": "first", "role": "user", "files": [
            {"type": "image", "url": NEW}]},
        {"id": "answer", "parentId": "second", "role": "assistant"},
    ]


def context():
    return extension.image_catalog(branch()), [NEW]


def request():
    return SimpleNamespace(state=SimpleNamespace())


class PolicyTests(unittest.TestCase):
    def test_local_source_references_accept_bare_ids_and_content_routes(self):
        self.assertEqual(extension.source_file_id(FILE_ID), FILE_ID)
        self.assertEqual(extension.source_file_id(FILE_URL), "upload-test")
        for source in [None, "", "../secret", "/etc/passwd", "file:///etc/passwd",
                       "https://example.org/photo.gif", "//host/path", "photo.gif",
                       "/api/v1/files/../content", "/api/v1/files/id/content?download=1",
                       "id/other", "x" * 129, ANIMATED_GIF]:
            with self.subTest(source_type=type(source).__name__):
                self.assertIsNone(extension.source_file_id(source))

    def test_native_file_type_gif_attachment_is_catalogued(self):
        self.assertEqual(extension.message_images({"files": [
            {"type": "file", "content_type": "image/gif", "url": FILE_ID, "id": FILE_ID}]}),
            [FILE_ID])

    def test_history_is_never_base64_decoded(self):
        body = {"messages": [{"role": "user", "content": [part(OLD)]}]}
        with patch.object(extension.base64, "b64decode", side_effect=AssertionError("History decoded")):
            result = extension.prune_messages(body)
        self.assertIsInstance(result["messages"][0]["content"], str)

    def test_repeated_large_history_hashes_each_uri_once(self):
        large = uri(b"x" * (1024 * 1024))
        body = {"messages": [{"role": "user", "content": [part(large)]} for _ in range(160)]}
        with patch.object(extension, "fingerprint", wraps=extension.fingerprint) as digest:
            extension.prune_messages(body)
        self.assertEqual(digest.call_count, 1)

    def test_history_processing_budget_is_explicit(self):
        body = {"messages": [{"role": "user", "content": [part(OLD)]}]}
        with patch.object(extension, "MAX_HISTORY_IMAGE_CHARS", 4), self.assertRaisesRegex(ValueError, "budget"):
            extension.prune_messages(body)

    def test_history_image_part_count_is_bounded(self):
        body = {"messages": [{"role": "user", "content": [part(OLD), part(OLD)]}]}
        with patch.object(extension, "MAX_HISTORY_IMAGE_PARTS", 1), self.assertRaisesRegex(ValueError, "image-part"):
            extension.prune_messages(body)

    def test_branch_excludes_sibling(self):
        messages = {message["id"]: message for message in branch()}
        messages["secret"] = {"id": "secret", "parentId": "first", "role": "user",
                              "files": [{"type": "image", "url": NEW}]}
        selected = extension.branch_messages({"history": {"messages": messages}}, "answer")
        self.assertNotIn("secret:1", extension.image_catalog(selected))

    def test_branch_rejects_missing_anchor_cycle_and_broken_parent(self):
        for messages, anchor in [({}, "answer"),
                                 ({"a": {"id": "a", "parentId": "a"}}, "a"),
                                 ({"a": {"id": "a", "parentId": "missing"}}, "a")]:
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                extension.branch_messages({"history": {"messages": messages}}, anchor)

    def test_manifest_excludes_untrusted_names(self):
        catalog = extension.image_catalog(branch())
        self.assertEqual(catalog["first:1"]["label"], "user message 1, image 1")

    def test_deduplicate_files_and_content(self):
        self.assertEqual(extension.message_images({"files": [{"type": "image", "url": OLD}],
                                                   "content": [part(OLD)]}), [OLD])

    def test_content_type_upload_supported(self):
        self.assertEqual(extension.message_images({"files": [
            {"content_type": "image/png", "url": OLD}]}), [OLD])

    def test_catalog_is_bounded(self):
        messages = [{"id": f"m{i}", "role": "user", "content": [part(OLD)]}
                    for i in range(120)]
        catalog = extension.image_catalog(messages)
        self.assertEqual(len(catalog), extension.MAX_REFERENCES)
        self.assertNotIn("m0:1", catalog)
        self.assertIn("m119:1", catalog)

    def test_pruning_does_not_mutate_source_or_remove_text(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "Keep this caption"}, part(OLD)]},
            {"role": "user", "content": [part(NEW)]}]}
        before = copy.deepcopy(body)
        result = extension.prune_messages(body, {extension.fingerprint(NEW)})
        self.assertEqual(body, before)
        self.assertEqual(result["messages"][0]["content"], [{"type": "text", "text": "Keep this caption"}])
        self.assertEqual(result["messages"][1]["content"], [part(NEW)])

    def test_synthetic_user_tool_images_do_not_become_latest(self):
        body = {"messages": [{"role": "user", "content": [part(NEW)]},
                             {"role": "user", "content": [part(OLD)]}]}
        result = extension.prune_messages(body, {extension.fingerprint(NEW)})
        self.assertIsInstance(result["messages"][1]["content"], str)

    def test_duplicate_images_sent_once(self):
        body = {"messages": [{"role": "user", "content": [part(NEW)]},
                             {"role": "user", "content": [part(NEW)]}]}
        result = extension.prune_messages(body, {extension.fingerprint(NEW)})
        self.assertIsInstance(result["messages"][0]["content"], str)
        self.assertEqual(result["messages"][1]["content"], [part(NEW)])

    def test_nested_tool_outputs_are_pruned(self):
        body = {"messages": [{"role": "assistant", "output": [
            {"type": "function_call_output", "output": [
                {"type": "input_text", "text": "Keep tool text"},
                {"type": "input_image", "image_url": OLD}]}]}]}
        result = extension.prune_messages(body)
        self.assertEqual(result["messages"][0]["output"][0]["output"],
                         [{"type": "input_text", "text": "Keep tool text"}])

    def test_stored_file_metadata_is_pruned(self):
        body = {"messages": [{"role": "user", "files": [
            {"content_type": "image/png", "url": OLD}, {"type": "file", "id": "doc"}]}]}
        self.assertEqual(extension.prune_messages(body)["messages"][0]["files"],
                         [{"type": "file", "id": "doc"}])

    def test_current_file_url_upload_survives_pruning(self):
        # URL-form input must survive just like resolved embedded bytes.
        allowed = {extension.fingerprint(FILE_URL), extension.raw_fingerprint(png())}
        body = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "What is this?"}, part(FILE_URL)],
            "files": [{"type": "image", "url": FILE_URL, "name": "photo.png"}]}]}
        result = extension.prune_messages(body, allowed)
        self.assertEqual(result["messages"][0]["content"],
                         [{"type": "text", "text": "What is this?"}, part(FILE_URL)])
        self.assertEqual(result["messages"][0]["files"],
                         [{"type": "image", "url": FILE_URL, "name": "photo.png"}])

    def test_historical_file_url_uploads_are_pruned(self):
        body = {"messages": [{"role": "user", "files": [
            {"type": "image", "url": "/api/v1/files/old/content"}]}]}
        self.assertEqual(extension.prune_messages(body)["messages"][0]["files"], [])

    def test_substituted_gif_parts_and_files_replaced(self):
        rendered = uri(png("gold"))
        allowed = {extension.fingerprint(rendered)}
        body = {"messages": [{"role": "user", "content": [part(FILE_URL)],
                              "files": [{"type": "image", "url": FILE_URL}]}]}
        group = {"number": 1, "frames": ({"url": rendered, "timestamp_ms": 0},)}
        result = extension.prune_messages(body, allowed, (), {FILE_URL: group})
        self.assertEqual(result["messages"][0]["content"], [
            part(rendered), {"type": "text", "text": extension.gif_frame_label(group, 0)},
            {"type": "text", "text": extension.GIF_SAMPLING_NOTICE}])
        self.assertEqual(result["messages"][0]["files"], [
            {"type": "image", "url": rendered, "content_type": "image/jpeg",
             "name": "gif-1-frame-1.jpg"}])

    def test_unsubstituted_gif_occurrence_fails_safe(self):
        rendered = uri(png("gold"))
        allowed = {extension.fingerprint(rendered)}
        body = {"messages": [{"role": "user", "content": [part(FILE_URL)]}]}
        group = {"number": 1, "frames": ({"url": rendered, "timestamp_ms": 0},)}
        result = extension.prune_messages(body, allowed, (), {"/api/v1/files/other/content": group})
        self.assertIsInstance(result["messages"][0]["content"], str)

    def test_gif_sampling_is_temporal_bounded_and_includes_endpoints(self):
        frames = extension.sample_gif(gif(8, duration=100))
        self.assertEqual([frame["timestamp_ms"] for frame in frames], [0, 200, 500, 700])
        self.assertTrue(all(frame["url"].startswith("data:image/jpeg;base64,") for frame in frames))
        self.assertEqual(len(extension.sample_gif(gif(), 4)), 2)
        self.assertEqual(len(extension.sample_gif(gif(8), 1)), 1)

    def test_gif_sampling_uses_variable_delays_not_frame_index(self):
        frames = extension.sample_gif(gif(8, duration=[100, 100, 1000, 100, 100, 100, 100, 100]))
        self.assertEqual([frame["timestamp_ms"] for frame in frames], [0, 200, 1200, 1600])

    def test_gif_sampling_static_non_gif_and_invalid_inputs(self):
        self.assertIsNone(extension.sample_gif(gif(1)))
        self.assertIsNone(extension.sample_gif(png()))
        for count in (0, 5, True, "4"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                extension.sample_gif(gif(), count)
        for raw in (b"", b"not an image", gif()[:20]):
            with self.subTest(raw_length=len(raw)), self.assertRaises((ValueError, OSError)):
                extension.sample_gif(raw)

    def test_gif_sampling_source_frames_and_decode_pixels_are_bounded(self):
        with patch.object(extension, "MAX_GIF_SOURCE_FRAMES", 2):
            self.assertEqual(len(extension.sample_gif(gif(2))), 2)
            with self.assertRaisesRegex(ValueError, "source-frame"):
                extension.sample_gif(gif(3))
        with patch.object(extension, "MAX_GIF_DECODE_PIXELS", 8 * 8 * 2 * 2):
            self.assertEqual(len(extension.sample_gif(gif(2))), 2)
            with self.assertRaisesRegex(ValueError, "decoding"):
                extension.sample_gif(gif(3))
        with patch.object(extension, "MAX_PIXELS", 16), self.assertRaisesRegex(ValueError, "pixel"):
            extension.sample_gif(gif())
        with patch.object(extension, "MAX_SOURCE_BYTES", 16), self.assertRaisesRegex(ValueError, "oversized"):
            extension.sample_gif(gif())

    def test_gif_samples_resize_and_preserve_source(self):
        raw = gif(size=(100, 50))
        before = bytes(raw)
        with patch.object(extension, "MAX_EDGE", 16):
            frames = extension.sample_gif(raw)
        for frame in frames:
            with Image.open(io.BytesIO(extension.decode_image_url(frame["url"]))) as image:
                self.assertEqual(image.size, (16, 8))
                self.assertEqual(image.format, "JPEG")
        self.assertEqual(raw, before)

    def test_gif_samples_composite_transparency_and_disposal(self):
        palette = [0, 0, 0, 255, 0, 0, 0, 0, 255] + [0] * (768 - 9)
        images = [Image.new("P", (16, 16), 0) for _ in range(2)]
        for image in images:
            image.putpalette(palette)
        images[0].paste(1, (0, 0, 8, 8))
        images[1].paste(2, (8, 8, 16, 16))
        output = io.BytesIO()
        images[0].save(output, "GIF", save_all=True, append_images=images[1:],
                       transparency=0, disposal=2, optimize=False, duration=100)
        samples = extension.sample_gif(output.getvalue())
        with Image.open(io.BytesIO(extension.decode_image_url(samples[1]["url"]))) as image:
            # The previous red patch must have been disposed, not overlaid.
            self.assertTrue(all(channel > 230 for channel in image.getpixel((2, 2))))
            red, green, blue = image.getpixel((12, 12))
            self.assertGreater(blue, 200)
            self.assertLess(max(red, green), 40)

    def test_render_first_gif_frame(self):
        rendered = extension.render_image(gif())
        with Image.open(io.BytesIO(extension.decode_image_url(rendered))) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (8, 8))
            red, green, blue = image.getpixel((0, 0))
            self.assertGreater(red, 200)
            self.assertLess(max(green, blue), 40)

    def test_entry_files_are_static_imports_of_the_shared_package(self):
        for name, exported in [("filter.py", "Filter"), ("tool.py", "Tools")]:
            with self.subTest(name=name):
                code = (Path(__file__).parent / name).read_text()
                tree = ast.parse(code)
                self.assertEqual(len(tree.body), 2)  # Frontmatter + one import.
                entry = tree.body[1]
                self.assertIsInstance(entry, ast.ImportFrom)
                self.assertEqual(entry.module, "chat_image_retrieval")
                self.assertEqual([alias.name for alias in entry.names], [exported])
                namespace = {}
                exec(compile(tree, name, "exec"), namespace)
                self.assertIs(namespace[exported], getattr(extension, exported))

    def test_invalid_sources_rejected(self):
        for url in ["file:///etc/passwd", "https://example.org/photo.png",
                    "data:image/svg+xml;base64,AAAA", "data:image/png;base64,not-base64"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                extension.decode_image_url(url)

    def test_encoded_byte_limit(self):
        with patch.object(extension, "MAX_SOURCE_BYTES", 4), self.assertRaises(ValueError):
            extension.decode_image_url(OLD)

    def test_render_resizes_and_strips_metadata(self):
        rendered = extension.render_image(png(size=(2000, 50)))
        with Image.open(io.BytesIO(extension.decode_image_url(rendered))) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertLessEqual(max(image.size), extension.MAX_EDGE)
            self.assertNotIn("exif", image.info)

    def test_pixel_limit_and_non_images(self):
        with patch.object(extension, "MAX_PIXELS", 5), self.assertRaises(ValueError):
            extension.render_image(png())
        with self.assertRaises(Exception):
            extension.render_image(b"not an image")

    def test_local_reader_denies_escape_and_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "uploads"
            root.mkdir()
            secret = Path(folder) / "outside"
            secret.write_bytes(b"secret")
            (root / "ok").write_bytes(b"image")
            (root / "escape").symlink_to(secret)
            (root / "parent").symlink_to(Path(folder), target_is_directory=True)
            self.assertEqual(extension.read_local_file(root / "ok", root), b"image")
            for path in [secret, root / ".." / "outside", root / "escape", root / "parent" / "outside"]:
                with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                    extension.read_local_file(path, root)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context_mock = AsyncMock(return_value=context())
        self.patcher = patch.object(extension, "owned_context", self.context_mock)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.response_mock = AsyncMock(return_value={})
        response_patcher = patch.object(extension, "response_images", self.response_mock)
        response_patcher.start()
        self.addCleanup(response_patcher.stop)
        self.req = request()
        self.filter = extension.Filter()
        self.tool = extension.Tools()
        await self.filter.inlet({"messages": []}, USER, META, self.req)

    async def retrieve(self, ref="first:1", approval=None):
        if approval is None:
            approval = AsyncMock(return_value=True)
        return await self.tool.retrieve_chat_image(ref, USER, META, self.req, approval)

    async def test_inlet_manifest_and_immutable_payload(self):
        body = {"messages": [{"role": "user", "content": [part(OLD), part(NEW)]}]}
        before = copy.deepcopy(body)
        result = await self.filter.inlet(body, USER, META, self.req)
        self.assertEqual(body, before)
        self.assertIn("first:1", result["messages"][0]["content"])
        self.assertNotIn("untrusted filename", result["messages"][0]["content"])
        self.assertEqual(result["messages"][1]["content"], [part(NEW)])

    async def test_manifest_preserves_existing_system_prompt(self):
        for content in ["Original safety instructions", [{"type": "text", "text": "Original safety instructions"}]]:
            body = {"messages": [{"role": "system", "content": content}]}
            result = await self.filter.inlet(body, USER, META, self.req)
            self.assertEqual(len(result["messages"]), 1)
            self.assertIn("Original safety instructions", str(result["messages"][0]["content"]))
            self.assertIn("first:1", str(result["messages"][0]["content"]))
            self.assertEqual(body["messages"][0]["content"], content)

    async def test_success_requires_approval_and_rechecks_access(self):
        approval = AsyncMock(return_value=True)
        result = await self.retrieve(approval=approval)
        self.assertTrue(result.startswith("data:image/jpeg;base64,"))
        self.assertEqual(approval.await_count, 1)
        self.assertEqual(self.context_mock.await_count, 3)  # inlet + before/after consent
        self.assertIn(extension.fingerprint(result),
                      extension.request_state(self.req, USER, META)["approved"])

    async def test_no_source_read_before_approval(self):
        with patch.object(extension, "load_source", AsyncMock(wraps=extension.load_source)) as load:
            for decision in [False, None, "true", {"error": "disconnected"}, 1]:
                await self.filter.inlet({"messages": []}, USER, META, self.req)
                load.reset_mock()
                result = await self.retrieve(approval=AsyncMock(return_value=decision))
                self.assertIn("declined", result)
                load.assert_not_awaited()

    async def test_wrong_ref_or_branch_never_prompts(self):
        approval = AsyncMock(return_value=True)
        for ref in ["../secret", "other:1", "https://example.org/image", "first:999"]:
            result = await self.retrieve(ref, approval)
            self.assertIn("denied", result)
        approval.assert_not_awaited()

    async def test_tool_without_filter_fails_closed(self):
        result = await self.tool.retrieve_chat_image("first:1", USER, META, request(), AsyncMock())
        self.assertIn("unavailable", result)

    async def test_request_binding_cannot_be_reused_by_another_user_or_chat(self):
        for user, metadata in [({"id": "other"}, META), (USER, {**META, "chat_id": "other"}),
                               (USER, {**META, "message_id": "other"})]:
            result = await self.tool.retrieve_chat_image("first:1", user, metadata,
                                                       self.req, AsyncMock(return_value=True))
            self.assertIn("unavailable", result)

    async def test_parallel_model_responses_do_not_share_approvals(self):
        other_meta = {**META, "message_id": "other-answer"}
        await self.filter.inlet({"messages": []}, USER, other_meta, self.req)
        retrieved = await self.retrieve()
        body = {"messages": [{"role": "user", "content": [part(retrieved)]}]}
        own = await self.filter.request(body, USER, META, self.req)
        other = await self.filter.request(body, USER, other_meta, self.req)
        self.assertEqual(own["messages"][0]["content"], [part(retrieved)])
        self.assertIsInstance(other["messages"][0]["content"], str)

    async def test_generated_and_other_current_tool_images_survive(self):
        generated, screenshot = uri(png("green")), uri(png("yellow"))
        self.response_mock.return_value = {"call:generate": (generated,), "call:screenshot": (screenshot,)}
        body = {"messages": [{"role": "user", "content": [part(OLD), part(generated), part(screenshot)]}]}
        result = await self.filter.request(body, USER, META, self.req)
        self.assertEqual(result["messages"][0]["content"], [part(generated), part(screenshot)])
        self.assertEqual(body["messages"][0]["content"][0], part(OLD))

    async def test_regenerating_identical_pixels_does_not_drop_images(self):
        self.response_mock.return_value = {"call:generate": (OLD, OLD)}
        body = {"messages": [{"role": "user", "content": [part(OLD), part(OLD)]}]}
        result = await self.filter.request(body, USER, META, self.req)
        self.assertEqual(result["messages"][0]["content"], [part(OLD), part(OLD)])

    async def test_generated_images_survive_nested_and_tool_content_formats(self):
        generated = uri(png("green"))
        self.response_mock.return_value = {"call:generate": (generated,)}
        for body in [
            {"messages": [{"role": "tool", "tool_call_id": "generate", "content": [
                {"type": "input_image", "image_url": generated}]}]},
            {"messages": [{"role": "assistant", "output": [
                {"type": "function_call_output", "call_id": "generate", "output": [
                    {"type": "input_image", "image_url": generated}]}]}]},
        ]:
            self.assertEqual(await self.filter.request(body, USER, META, self.req), body)

    async def test_generated_display_metadata_and_json_untouched(self):
        # Native generate_image returns JSON + a UI files event, not image bytes.
        text = '{"status":"success","images":[{"url":"/api/v1/files/gen/content"}]}'
        body = {"messages": [{"role": "tool", "content": text}],
                "files": [{"type": "image", "url": "/api/v1/files/gen/content"}]}
        self.assertEqual(await self.filter.request(body, USER, META, self.req), body)

    async def test_current_generated_message_file_metadata_preserved(self):
        generated = uri(png("green"))
        self.response_mock.return_value = {"file:generate": (generated,)}
        body = {"messages": [{"role": "assistant", "files": [
            {"type": "image", "url": generated, "name": "generated.png"}]}]}
        self.assertEqual(await self.filter.request(body, USER, META, self.req), body)

    async def test_retrieval_size_limits_do_not_resize_or_drop_generated_images(self):
        generated = uri(png("green"))
        self.response_mock.return_value = {"call:generate": (generated,)}
        body = {"messages": [{"role": "user", "content": [part(generated)]}]}
        with patch.object(extension, "MAX_SOURCE_BYTES", 4), \
             patch.object(extension, "render_image", side_effect=AssertionError("Generation was resized")):
            self.assertEqual(await self.filter.request(body, USER, META, self.req), body)

    async def test_old_tool_result_is_not_current_on_resume(self):
        self.response_mock.return_value = {"call:old-generation": (OLD,)}
        await self.filter.inlet({"messages": []}, USER, META, self.req)
        body = {"messages": [{"role": "user", "content": [part(OLD)]}]}
        result = await self.filter.request(body, USER, META, self.req)
        self.assertIsInstance(result["messages"][0]["content"], str)
        self.response_mock.return_value = {"call:old-generation": (OLD,), "call:new-generation": (OLD,)}
        result = await self.filter.request(body, USER, META, self.req)
        self.assertEqual(result["messages"][0]["content"], [part(OLD)])

    async def test_generated_sources_resolved_once_across_tool_rounds(self):
        generated = uri(png("green"))
        self.response_mock.return_value = {"call:generate": (generated,)}
        body = {"messages": [{"role": "user", "content": [part(generated)]}]}
        with patch.object(extension, "response_source_keys", wraps=extension.response_source_keys) as sources:
            await self.filter.request(body, USER, META, self.req)
            await self.filter.request(body, USER, META, self.req)
        self.assertEqual(sources.call_count, 1)

    async def test_pruning_runs_off_the_async_event_loop(self):
        loop_thread = threading.get_ident()
        original = extension.prune_messages

        def checked(*args):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            return original(*args)

        with patch.object(extension, "prune_messages", side_effect=checked):
            await self.filter.inlet({"messages": []}, USER, META, self.req)
            await self.filter.request({"messages": []}, USER, META, self.req)

    async def test_current_generated_image_does_not_leak_to_another_response(self):
        generated = uri(png("green"))
        other_meta = {**META, "message_id": "other-answer"}
        await self.filter.inlet({"messages": []}, USER, other_meta, self.req)
        self.response_mock.side_effect = lambda user, metadata: (
            {"call:generate": (generated,)} if metadata == META else {})
        body = {"messages": [{"role": "user", "content": [part(generated)]}]}
        own = await self.filter.request(body, USER, META, self.req)
        other = await self.filter.request(body, USER, other_meta, self.req)
        self.assertEqual(own["messages"][0]["content"], [part(generated)])
        self.assertIsInstance(other["messages"][0]["content"], str)

    async def test_access_revoked_during_approval(self):
        self.context_mock.side_effect = [context(), ValueError("private server path")]
        with patch.object(extension, "load_source", AsyncMock()) as load:
            result = await self.retrieve()
            self.assertIn("unavailable", result)
            self.assertNotIn("private", result)
            load.assert_not_awaited()

    async def test_changed_attachment_during_approval(self):
        changed, latest = context()
        changed["first:1"] = {"url": NEW, "label": "changed"}
        self.context_mock.side_effect = [context(), (changed, latest)]
        self.assertIn("changed", await self.retrieve())

    async def test_approval_timeout_and_missing_callback(self):
        approval = AsyncMock(side_effect=asyncio.TimeoutError)
        self.assertIn("unavailable", await self.retrieve(approval=approval))
        result = await self.tool.retrieve_chat_image("first:1", USER, META, self.req)
        self.assertIn("approval is unavailable", result)

    async def test_concurrent_calls_bounded(self):
        results = await asyncio.gather(*(self.retrieve() for _ in range(6)))
        self.assertEqual(sum(result.startswith("data:") for result in results), 3)
        self.assertEqual(sum("limit" in result for result in results), 3)

    async def test_retrieved_image_only_survives_current_request(self):
        retrieved = await self.retrieve()
        body = {"messages": [{"role": "user", "content": [part(NEW)]},
                             {"role": "user", "content": [part(retrieved)]}]}
        current = await self.filter.request(body, USER, META, self.req)
        self.assertEqual(current["messages"][1]["content"], [part(retrieved)])
        next_request = request()
        next_turn = await self.filter.inlet(body, USER, META, next_request)
        self.assertIsInstance(next_turn["messages"][-1]["content"], str)

    async def test_oversized_latest_set_denied(self):
        self.context_mock.return_value = ({}, [NEW] * 9)
        with self.assertRaisesRegex(ValueError, "eight"):
            await self.filter.inlet({"messages": []}, USER, META, self.req)

    async def test_current_photo_upload_survives_inlet(self):
        # Saved metadata and some filter inputs use upload URLs. Keep both
        # the image content part and its matching files entry.
        self.context_mock.return_value = ({}, [FILE_URL])
        with patch.object(extension, "load_source", AsyncMock(return_value=png())) as load:
            body = {"model": "mistral.mistral-large-4", "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}, part(FILE_URL)],
                 "files": [{"type": "image", "url": FILE_URL}]}]}
            result = await self.filter.inlet(body, USER, META, self.req)
            load.assert_awaited_once_with(FILE_URL, "owner")
        self.assertEqual(result["messages"][0]["content"],
                         [{"type": "text", "text": "hi"}, part(FILE_URL)])
        self.assertEqual(result["messages"][0]["files"], [{"type": "image", "url": FILE_URL}])

    async def test_eight_normal_images_remain_unchanged_on_text_followups(self):
        sources = [uri(png(color)) for color in (
            "red", "blue", "green", "yellow", "purple", "orange", "cyan", "white")]
        self.context_mock.return_value = ({}, sources)
        body = {"messages": [
            {"role": "user", "content": [part(url) for url in sources],
             "files": [{"type": "file", "content_type": "image/png", "url": url,
                        "name": f"photo-{index}.png"} for index, url in enumerate(sources)]},
            {"role": "assistant", "content": "I see eight pictures."},
            {"role": "user", "content": "Compare their colors."}]}
        before = copy.deepcopy(body)
        result = await self.filter.inlet(body, USER, META, self.req)
        self.assertEqual(result, before)
        self.assertEqual(await self.filter.request(result, USER, META, self.req), before)
        self.assertEqual(body, before)

    async def test_new_normal_image_set_replaces_the_older_set(self):
        self.context_mock.return_value = ({}, [NEW])
        body = {"messages": [{"role": "user", "content": [part(OLD)],
                              "files": [{"type": "image", "url": OLD}]},
                             {"role": "user", "content": [part(NEW)],
                              "files": [{"type": "image", "url": NEW}]}]}
        result = await self.filter.inlet(body, USER, META, self.req)
        self.assertFalse(extension.message_images(result["messages"][0]))
        self.assertEqual(result["messages"][1], body["messages"][1])

    async def test_oversized_normal_image_fails_closed(self):
        self.context_mock.return_value = ({}, [NEW])
        with patch.object(extension, "MAX_SOURCE_BYTES", 16), self.assertRaisesRegex(ValueError, "10 MiB"):
            await self.filter.inlet({"messages": []}, USER, META, self.req)

    async def test_animated_gif_expands_to_available_samples(self):
        self.context_mock.return_value = ({}, [ANIMATED_GIF])
        body = {"model": "mistral-large-4", "messages": [
            {"role": "user", "content": [part(ANIMATED_GIF)],
             "files": [{"type": "image", "url": ANIMATED_GIF}]}]}
        result = await self.filter.inlet(body, USER, META, self.req)
        content = result["messages"][0]["content"]
        self.assertTrue(content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertNotIn(ANIMATED_GIF, json.dumps(result))
        state = extension.request_state(self.req, USER, META)
        self.assertEqual(len(state["substitutions"][ANIMATED_GIF]["frames"]), 2)
        self.assertEqual(len(extension.message_images(result["messages"][0])), 2)
        # Substitution persists across tool rounds via the request filter.
        again = {"model": "mistral-large-4", "messages": [
            {"role": "user", "content": [part(ANIMATED_GIF)]}]}
        rerun = await self.filter.request(again, USER, META, self.req)
        self.assertTrue(rerun["messages"][0]["content"][0]["image_url"]["url"]
                        .startswith("data:image/jpeg;base64,"))

    async def test_animated_gif_sampled_for_non_mistral_model(self):
        self.context_mock.return_value = ({}, [ANIMATED_GIF])
        body = {"model": "gpt-6-luna", "messages": [
            {"role": "user", "content": [part(ANIMATED_GIF)]}]}
        result = await self.filter.inlet(body, USER, META, self.req)
        self.assertNotIn(ANIMATED_GIF, json.dumps(result))
        self.assertEqual(len(extension.message_images(result["messages"][0])), 2)

    async def test_file_url_gif_resolved_to_base64_is_substituted_across_rounds(self):
        self.context_mock.return_value = ({}, [FILE_URL])
        # Saved metadata keeps the upload URL; OWUI has already resolved the
        # provider content to base64. MIME aliases must not change identity.
        resolved = ANIMATED_GIF.replace("image/gif", "image/png")
        body = {"model": "deepseek-v4.1-flash-mistral", "messages": [
            {"role": "user", "content": [part(resolved)],
             "files": [{"type": "image", "url": FILE_URL}]}]}
        before = copy.deepcopy(body)
        with patch.object(extension, "load_source", AsyncMock(return_value=gif())):
            result = await self.filter.inlet(body, USER, META, self.req)
        rendered = result["messages"][0]["content"][0]["image_url"]["url"]
        self.assertTrue(rendered.startswith("data:image/jpeg;base64,"))
        self.assertEqual(result["messages"][0]["files"][0]["url"], rendered)
        self.assertEqual(body, before)
        self.assertEqual(await self.filter.request(body, USER, META, self.req), result)
        self.assertEqual(await self.filter.request(result, USER, META, self.req), result)

    async def test_gif_frame_count_valve_can_select_one(self):
        self.filter.valves.gif_max_frames = 1
        self.context_mock.return_value = ({}, [ANIMATED_GIF])
        body = {"model": "mistral-large-4", "messages": [
            {"role": "user", "content": [part(ANIMATED_GIF)]}]}
        result = await self.filter.inlet(body, USER, META, self.req)
        self.assertEqual(len(extension.message_images(result["messages"][0])), 1)

    async def test_gif_expansion_budget_fails_closed(self):
        sources = [gif_uri(gif(4, size=(8 + index, 8))) for index in range(5)]
        self.context_mock.return_value = ({}, sources)
        body = {"messages": [{"role": "user", "content": [part(url) for url in sources]}]}
        with self.assertRaisesRegex(ValueError, "expand to 20.*limit is 16"):
            await self.filter.inlet(body, USER, META, self.req)
        self.filter.valves.gif_max_frames = 3
        result = await self.filter.inlet(body, USER, META, self.req)
        self.assertEqual(len(extension.message_images(result["messages"][0])), 15)
        self.assertEqual(await self.filter.request(result, USER, META, self.req), result)

    async def test_gif_decode_limit_fails_closed(self):
        self.context_mock.return_value = ({}, [ANIMATED_GIF])
        with patch.object(extension, "MAX_GIF_SOURCE_FRAMES", 1), self.assertRaisesRegex(ValueError, "GIFs must also fit"):
            await self.filter.inlet({"messages": []}, USER, META, self.req)

    async def test_sampling_configuration_limits_are_validated_at_inlet(self):
        for key, invalid in (("gif_max_frames", 0), ("gif_max_frames", 5),
                             ("gif_max_frames", True), ("max_expanded_images", 0),
                             ("max_expanded_images", 33)):
            with self.subTest(key=key, value=invalid):
                self.filter.valves = SimpleNamespace(gif_max_frames=4, max_expanded_images=16)
                setattr(self.filter.valves, key, invalid)
                with self.assertRaises(ValueError):
                    await self.filter.inlet({"messages": []}, USER, META, self.req)

    async def test_gif_sampling_runs_off_the_event_loop(self):
        self.context_mock.return_value = ({}, [ANIMATED_GIF])
        loop_thread = threading.get_ident()
        original = extension.sample_gif

        def checked(*args):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            return original(*args)

        with patch.object(extension, "sample_gif", side_effect=checked):
            await self.filter.inlet({"messages": []}, USER, META, self.req)

    async def test_same_start_frames_in_two_gifs_remain_distinct_and_idempotent(self):
        longer = gif_uri(gif(4))
        self.context_mock.return_value = ({}, [ANIMATED_GIF, longer])
        body = {"messages": [{"role": "user", "content": [part(ANIMATED_GIF), part(longer)],
                              "files": [{"type": "image", "url": url}
                                        for url in (ANIMATED_GIF, longer)]}]}
        result = await self.filter.inlet(body, USER, META, self.req)
        content = result["messages"][0]["content"]
        self.assertEqual(sum(item.get("type") == "image_url" for item in content), 6)
        self.assertEqual(len(result["messages"][0]["files"]), 6)
        self.assertEqual(await self.filter.request(result, USER, META, self.req), result)

    async def test_repeated_pixel_samples_keep_their_separate_timestamps(self):
        images = [Image.new("RGB", (8, 8), color) for color in ("red", "blue", "red", "blue")]
        output = io.BytesIO()
        images[0].save(output, "GIF", save_all=True, append_images=images[1:], duration=100)
        source = gif_uri(output.getvalue())
        self.context_mock.return_value = ({}, [source])
        body = {"messages": [{"role": "user", "content": [part(source)]}]}
        result = await self.filter.inlet(body, USER, META, self.req)
        content = result["messages"][0]["content"]
        self.assertEqual(sum(item.get("type") == "image_url" for item in content), 4)
        self.assertIn("timestamp 0.200 s", json.dumps(content))
        self.assertEqual(await self.filter.request(result, USER, META, self.req), result)

    async def test_mixed_content_and_files_only_gif_groups_are_idempotent(self):
        other_gif = gif_uri(gif(size=(10, 10)))
        self.context_mock.return_value = ({}, [ANIMATED_GIF, other_gif])
        body = {"messages": [{"role": "user", "content": [part(ANIMATED_GIF)],
                              "files": [{"type": "image", "url": other_gif}]}]}
        result = await self.filter.inlet(body, USER, META, self.req)
        content = result["messages"][0]["content"]
        self.assertEqual(sum(item.get("type") == "image_url" for item in content), 4)
        self.assertEqual(json.dumps(content).count(extension.GIF_SAMPLING_NOTICE), 1)
        self.assertEqual(await self.filter.request(result, USER, META, self.req), result)

    async def test_gif_mixtures_normalize_idempotently_across_reconstruction(self):
        gifs = [gif_uri(gif(count, size=(8 + index, 8), duration=100))
                for index, count in enumerate((2, 4, 6))]
        photo = extension.sample_gif(gif(2, duration=100))[0]["url"]
        for count in range(1, 4):
            for with_photo in (False, True):
                for files_only in (False, True):
                    with self.subTest(gifs=count, photo=with_photo, files_only=files_only):
                        sources = gifs[:count] + ([photo] if with_photo else [])
                        self.context_mock.return_value = ({}, sources)
                        content = [{"type": "text", "text": "Compare these images."}]
                        if not files_only:
                            content += [part(url) for url in sources]
                        body = {"messages": [{"role": "user", "content": content,
                                              "files": [{"type": "image", "url": url} for url in sources]}]}
                        req = request()
                        result = await self.filter.inlet(body, USER, META, req)
                        self.assertEqual(await self.filter.request(result, USER, META, req), result)
                        rebuilt = copy.deepcopy(result)
                        rebuilt["messages"][0].pop("files")
                        normalized = await self.filter.request(rebuilt, USER, META, req)
                        self.assertEqual(normalized, rebuilt)

    async def test_normal_photo_identical_to_gif_sample_is_not_expanded(self):
        photo = extension.sample_gif(gif())[0]["url"]
        self.context_mock.return_value = ({}, [ANIMATED_GIF, photo])
        photo_file = {"type": "image", "content_type": "image/jpeg", "url": photo, "name": "photo.jpg"}
        body = {"messages": [{"role": "user", "content": [part(ANIMATED_GIF), part(photo)],
                              "files": [{"type": "image", "url": ANIMATED_GIF}, photo_file]}]}
        result = await self.filter.inlet(body, USER, META, self.req)
        content = result["messages"][0]["content"]
        self.assertEqual(sum(item.get("type") == "image_url" for item in content), 3)
        self.assertEqual(result["messages"][0]["files"][-1], photo_file)
        self.assertEqual(await self.filter.request(result, USER, META, self.req), result)

    async def test_fresh_tool_image_identical_to_sample_is_preserved(self):
        self.context_mock.return_value = ({}, [ANIMATED_GIF])
        body = {"messages": [{"role": "user", "content": [part(ANIMATED_GIF)],
                              "files": [{"type": "image", "url": ANIMATED_GIF}]}]}
        result = await self.filter.inlet(body, USER, META, self.req)
        generated = extension.sample_gif(gif())[0]["url"]
        self.response_mock.return_value = {"call:generate": (generated,)}
        fresh = {"role": "user", "content": [part(generated)],
                 "files": [{"type": "image", "url": generated, "name": "generated.jpg"}]}
        result["messages"].append(fresh)
        rerun = await self.filter.request(result, USER, META, self.req)
        self.assertEqual(rerun, result)
        self.assertEqual(rerun["messages"][-1], fresh)

    async def test_sampled_frames_do_not_leak_into_the_next_turn(self):
        self.context_mock.return_value = ({}, [ANIMATED_GIF])
        body = {"messages": [{"role": "user", "content": [part(ANIMATED_GIF)]}]}
        result = await self.filter.inlet(body, USER, META, self.req)
        self.context_mock.return_value = ({}, [NEW])
        later = await self.filter.inlet(result, USER, META, request())
        self.assertFalse(extension.message_images(later["messages"][0]))

    async def test_nested_gif_samples_keep_native_input_image_shape(self):
        self.context_mock.return_value = ({}, [ANIMATED_GIF])
        body = {"messages": [{"role": "user", "content": [part(ANIMATED_GIF)]}]}
        await self.filter.inlet(body, USER, META, self.req)
        nested = {"messages": [{"role": "assistant", "output": [
            {"type": "function_call_output", "output": [
                {"type": "input_image", "image_url": ANIMATED_GIF}]}]}]}
        result = await self.filter.request(nested, USER, META, self.req)
        output = result["messages"][0]["output"][0]["output"]
        self.assertEqual([item["type"] for item in output],
                         ["input_image", "input_text", "input_image", "input_text", "input_text"])
        self.assertEqual(output[-1]["text"], extension.GIF_SAMPLING_NOTICE)
        self.assertEqual(await self.filter.request(result, USER, META, self.req), result)

    async def test_native_bare_id_gif_attachment_is_substituted(self):
        self.context_mock.return_value = ({}, [FILE_ID])
        body = {"model": "mistral-large-4", "messages": [
            {"role": "user", "content": [part(ANIMATED_GIF)], "files": [
                {"type": "file", "content_type": "image/gif", "url": FILE_ID, "id": FILE_ID}]}]}
        with patch.object(extension, "load_source", AsyncMock(return_value=gif())) as load:
            result = await self.filter.inlet(body, USER, META, self.req)
            load.assert_awaited_once_with(FILE_ID, "owner")
        frame = result["messages"][0]["content"][0]["image_url"]["url"]
        self.assertTrue(frame.startswith("data:image/jpeg;base64,"))
        self.assertEqual(result["messages"][0]["files"][0]["url"], frame)
        self.assertEqual(await self.filter.request(body, USER, META, self.req), result)

    async def test_current_tool_gif_is_unchanged_even_if_it_matches_upload(self):
        self.context_mock.return_value = ({}, [ANIMATED_GIF])
        await self.filter.inlet({"model": "mistral-large-4", "messages": []}, USER, META, self.req)
        self.response_mock.return_value = {"call:generate": (ANIMATED_GIF,)}
        body = {"messages": [{"role": "user", "content": [part(ANIMATED_GIF)],
                              "files": [{"type": "image", "url": ANIMATED_GIF}]}]}
        self.assertEqual(await self.filter.request(body, USER, META, self.req), body)

    async def test_gif_disclosure_and_metadata_are_request_only_and_idempotent(self):
        self.context_mock.return_value = ({}, [FILE_ID])
        attachment = {"type": "file", "content_type": "image/gif", "url": FILE_ID,
                      "id": FILE_ID, "name": "original-animation.gif", "size": 123,
                      "file": {"id": FILE_ID, "filename": "original-animation.gif",
                               "meta": {"content_type": "image/gif"}}}
        document = {"type": "file", "url": "document-id", "name": "notes.txt"}
        body = {"model": "mistral-large-4", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "do you see that?"},
                                         part(ANIMATED_GIF)],
             "files": [attachment, document]}]}
        before = copy.deepcopy(body)
        with patch.object(extension, "load_source", AsyncMock(return_value=gif())):
            result = await self.filter.inlet(body, USER, META, self.req)
        message = result["messages"][0]
        frame = message["content"][1]["image_url"]["url"]
        group = extension.request_state(self.req, USER, META)["substitutions"][FILE_ID]
        self.assertEqual(message["files"], [
            {"type": "image", "content_type": "image/jpeg", "url": sample["url"],
             "name": f"gif-1-frame-{index}.jpg"}
            for index, sample in enumerate(group["frames"], 1)] + [document])
        notice = {"type": "text", "text": extension.GIF_SAMPLING_NOTICE}
        self.assertEqual(message["content"].count(notice), 1)
        self.assertNotIn(FILE_ID, json.dumps(result))
        self.assertNotIn("original-animation.gif", json.dumps(result))
        self.assertEqual(body, before)
        for source in (body, result):
            self.assertEqual(await self.filter.request(source, USER, META, self.req), result)
        # Native tool-loop reconstruction can drop files and the injected note.
        rebuilt = {"model": body["model"], "messages": [
            {"role": "user", "content": [part(frame)]}]}
        restored = await self.filter.request(rebuilt, USER, META, self.req)
        self.assertEqual(restored["messages"][0]["content"], message["content"][1:])
        self.assertEqual(await self.filter.request(restored, USER, META, self.req), restored)

    async def test_multiple_converted_gifs_have_one_notice_and_leave_photo_unchanged(self):
        other_gif = gif_uri(gif(size=(10, 10)))
        self.context_mock.return_value = ({}, [ANIMATED_GIF, other_gif, NEW])
        body = {"model": "mistral", "messages": [
            {"role": "user", "content": [part(ANIMATED_GIF), part(other_gif), part(NEW)],
             "files": [{"type": "image", "url": NEW, "name": "photo.png"}]}]}
        result = await self.filter.inlet(body, USER, META, self.req)
        self.assertEqual(result["messages"][0]["files"], body["messages"][0]["files"])
        self.assertIn(part(NEW), result["messages"][0]["content"])
        self.assertEqual(json.dumps(result).count(extension.GIF_SAMPLING_NOTICE), 1)
        self.assertEqual(await self.filter.request(result, USER, META, self.req), result)

    async def test_files_only_gif_gets_disclosure_without_mutating_user_text(self):
        self.context_mock.return_value = ({}, [FILE_ID])
        body = {"model": "mistral", "messages": [
            {"role": "user", "content": "look at this", "files": [
                {"type": "file", "content_type": "image/gif", "url": FILE_ID}]}]}
        with patch.object(extension, "load_source", AsyncMock(return_value=gif())):
            result = await self.filter.inlet(body, USER, META, self.req)
        self.assertEqual(result["messages"][0]["content"][0], {"type": "text", "text": "look at this"})
        self.assertEqual(len(extension.message_images(result["messages"][0])), 2)
        self.assertIn({"type": "text", "text": extension.GIF_SAMPLING_NOTICE},
                      result["messages"][0]["content"])
        self.assertEqual(body["messages"][0]["content"], "look at this")
        self.assertEqual(await self.filter.request(result, USER, META, self.req), result)

    async def test_static_gif_not_substituted(self):
        self.context_mock.return_value = ({}, [STATIC_GIF])
        body = {"model": "mistral-large-4", "messages": [
            {"role": "user", "content": [part(STATIC_GIF)]}]}
        result = await self.filter.inlet(body, USER, META, self.req)
        self.assertEqual(result["messages"][0]["content"], [part(STATIC_GIF)])

    async def test_gif_retrieval_renders_first_frame(self):
        catalog, latest = context()
        catalog["first:1"] = {"url": ANIMATED_GIF, "label": "user message 1, image 1"}
        self.context_mock.return_value = (catalog, latest)
        await self.filter.inlet({"messages": []}, USER, META, self.req)
        result = await self.retrieve()
        self.assertTrue(result.startswith("data:image/jpeg;base64,"))

    async def test_external_source_is_not_fetched(self):
        for url in ["https://127.0.0.1/private", "file:///etc/passwd", "/etc/passwd",
                    "/api/v1/files/../content", "//host/api/v1/files/a/content"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                await extension.load_source(url, "owner")


if __name__ == "__main__":
    unittest.main()
