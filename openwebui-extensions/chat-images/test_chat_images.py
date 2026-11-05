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


def gif(frames=2, size=(8, 8)):
    images = [Image.new("RGB", size, color) for color in ("red", "blue")[:frames]]
    output = io.BytesIO()
    images[0].save(output, "GIF", save_all=True, append_images=images[1:])
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
        result = extension.prune_messages(body, allowed, (), {FILE_URL: rendered})
        self.assertEqual(result["messages"][0]["content"], [part(rendered)])
        self.assertEqual(result["messages"][0]["files"], [{"type": "image", "url": rendered}])

    def test_unsubstituted_gif_occurrence_fails_safe(self):
        rendered = uri(png("gold"))
        allowed = {extension.fingerprint(rendered)}
        body = {"messages": [{"role": "user", "content": [part(FILE_URL)]}]}
        result = extension.prune_messages(body, allowed, (), {"/api/v1/files/other/content": rendered})
        self.assertIsInstance(result["messages"][0]["content"], str)

    def test_model_scope_matching(self):
        self.assertTrue(extension.model_in_scope("mistral.mistral-large-4", "mistral"))
        self.assertTrue(extension.model_in_scope("deepseek-v4.1-flash-mistral", "Mistral"))
        self.assertTrue(extension.model_in_scope("mistral-large-4", " mistral , other"))
        self.assertFalse(extension.model_in_scope("gpt-6-luna", "mistral"))
        self.assertFalse(extension.model_in_scope(None, "mistral"))
        self.assertFalse(extension.model_in_scope("mistral-large-4", ""))
        self.assertFalse(extension.model_in_scope("mistral-large-4", " , "))

    def test_animated_gif_probe(self):
        self.assertTrue(extension.is_animated_gif(extension.decode_image_url(ANIMATED_GIF)))
        self.assertFalse(extension.is_animated_gif(extension.decode_image_url(STATIC_GIF)))
        self.assertFalse(extension.is_animated_gif(b""))
        self.assertFalse(extension.is_animated_gif(b"not an image"))

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

    async def test_animated_gif_first_frame_substituted_for_scoped_model(self):
        self.context_mock.return_value = ({}, [ANIMATED_GIF])
        body = {"model": "mistral-large-4", "messages": [
            {"role": "user", "content": [part(ANIMATED_GIF)],
             "files": [{"type": "image", "url": ANIMATED_GIF}]}]}
        result = await self.filter.inlet(body, USER, META, self.req)
        content = result["messages"][0]["content"]
        self.assertTrue(content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertNotIn(ANIMATED_GIF, json.dumps(result))
        state = extension.request_state(self.req, USER, META)
        self.assertTrue(state["substitutions"][ANIMATED_GIF].startswith("data:image/jpeg"))
        # Substitution persists across tool rounds via the request filter.
        again = {"model": "mistral-large-4", "messages": [
            {"role": "user", "content": [part(ANIMATED_GIF)]}]}
        rerun = await self.filter.request(again, USER, META, self.req)
        self.assertTrue(rerun["messages"][0]["content"][0]["image_url"]["url"]
                        .startswith("data:image/jpeg;base64,"))

    async def test_animated_gif_kept_for_out_of_scope_model(self):
        self.context_mock.return_value = ({}, [ANIMATED_GIF])
        body = {"model": "gpt-6-luna", "messages": [
            {"role": "user", "content": [part(ANIMATED_GIF)]}]}
        result = await self.filter.inlet(body, USER, META, self.req)
        self.assertEqual(result["messages"][0]["content"], [part(ANIMATED_GIF)])

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

    async def test_first_frame_valve_can_be_disabled(self):
        self.filter.valves.gif_first_frame_models = ""
        self.context_mock.return_value = ({}, [ANIMATED_GIF])
        body = {"model": "mistral-large-4", "messages": [
            {"role": "user", "content": [part(ANIMATED_GIF)]}]}
        self.assertEqual(await self.filter.inlet(body, USER, META, self.req), body)

    async def test_current_tool_gif_is_unchanged_even_if_it_matches_upload(self):
        self.context_mock.return_value = ({}, [ANIMATED_GIF])
        await self.filter.inlet({"model": "mistral-large-4", "messages": []}, USER, META, self.req)
        self.response_mock.return_value = {"call:generate": (ANIMATED_GIF,)}
        body = {"messages": [{"role": "user", "content": [part(ANIMATED_GIF)],
                              "files": [{"type": "image", "url": ANIMATED_GIF}]}]}
        self.assertEqual(await self.filter.request(body, USER, META, self.req), body)

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
