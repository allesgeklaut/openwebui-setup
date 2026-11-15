import asyncio
import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import comfyui_media as media

NAME = "ltx25_mcp_0123456789_1024x576_73f_00001_.mp4"
# Encoded identities only; guidance must not decode or alter poster bytes.
POSTER = "data:image/png;base64,cG9zdGVy"
OTHER = "data:image/png;base64,b3RoZXI="
USER = {"id": "owner"}
META = {"chat_id": "chat", "message_id": "answer"}
FILE = {"id": "00000000-0000-4000-8000-000000000002",
        "type": "file", "content_type": "video/mp4",
        "url": "/api/v1/files/00000000-0000-4000-8000-000000000002/content",
        media.SOURCE_KEY: NAME}


def output(name="comfyui_generate_video", filename=NAME):
    return [
        {"type": "function_call", "call_id": "call", "name": name},
        {"type": "function_call_output", "call_id": "call", "status": "completed",
         "output": [{"type": "input_text", "text": "- video: " + media.HOST_ROOT + filename}]}]


def display_output(displayed=True, path=FILE["url"]):
    return [
        {"type": "function_call", "call_id": "display", "name": "display_file"},
        {"type": "function_call_output", "call_id": "display", "status": "completed",
         "output": [{"type": "input_text", "text": json.dumps({
             "source": "open_webui", "content_type": "video/mp4",
             "displayed": displayed, "path": path})}]}]


class PolicyTests(unittest.TestCase):
    def test_paired_completed_comfyui_results_only(self):
        self.assertEqual(media.video_sources(output()), [NAME])
        self.assertEqual(media.video_sources(output("comfyui_generate_video_from_image")), [NAME])
        for items in [output("other_tool"), output()[1:], [
            *output()[:1], {**output()[1], "status": "failed"}]]:
            self.assertEqual(media.video_sources(items), [])
        missing = output()
        for item in missing:
            item.pop("call_id")
        self.assertEqual(media.video_sources(missing), [])

    def test_sources_are_deduplicated_and_bounded(self):
        self.assertEqual(media.video_sources(output() * 6), [NAME])
        items = output()
        items[1]["output"] = [{"type": "input_text", "text": "\n".join(
            "- video: " + media.HOST_ROOT + NAME.replace("0123456789", f"{i:010x}")
            for i in range(8))}]
        self.assertEqual(len(media.video_sources(items)), media.MAX_VIDEOS)

    def test_no_arbitrary_paths_urls_or_traversal(self):
        for name in ["../" + NAME, "/etc/passwd", NAME + "?secret", NAME + "x\rmalformed",
                     "https://host/video.mp4", "a.mp4", "staged/" + NAME]:
            self.assertEqual(media.video_sources(output(filename=name)), [])
        # A CR is a valid line separator, not part of the local filename.
        self.assertEqual(media.video_sources(output(filename=NAME + "\r")), [NAME])

    def test_bounded_mp4_read_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / NAME
            path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"video" * 10)
            with patch.object(media, "MEDIA_ROOT", folder):
                self.assertEqual(media.read_video(NAME), path.read_bytes())
                with patch.object(media, "MAX_VIDEO_BYTES", 4), self.assertRaises(ValueError):
                    media.read_video(NAME)
                path.write_bytes(b"not an mp4 file")
                with self.assertRaises(ValueError):
                    media.read_video(NAME)
                path.unlink()
                (root / "target").write_bytes(b"\x00\x00\x00\x18ftypisom")
                path.symlink_to(root / "target")
                with self.assertRaises(OSError):
                    media.read_video(NAME)


class FilterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.message = {"role": "assistant", "output": output(), "files": []}
        self.owned = AsyncMock(return_value=self.message)
        self.upload = AsyncMock(return_value=FILE)

        async def persist(*args):
            self.message["files"].append(FILE)
            return self.message["files"]

        self.persist = AsyncMock(side_effect=persist)
        for name, mock in [("owned_response", self.owned), ("upload_video", self.upload),
                           ("persist_video", self.persist),
                           ("read_video", lambda _: b"\x00\x00\x00\x18ftypisom")]:
            p = patch.object(media, name, mock)
            p.start()
            self.addCleanup(p.stop)
        self.req = SimpleNamespace(state=SimpleNamespace())
        self.emitter = AsyncMock()
        self.filter = media.Filter()

    async def run_request(self, body=None):
        return await self.filter.request(
            body or {"messages": []}, USER, META, self.req, self.emitter)

    async def test_upload_persist_and_emit_once_across_rounds(self):
        body = {"messages": [{"role": "tool", "content": "generated"}],
                "metadata": {"live": object()}}
        first = await self.run_request(body)
        second = await self.run_request(body)
        self.assertIs(first["metadata"], body["metadata"])
        self.assertIn(FILE["url"], first["messages"][-1]["content"])
        self.assertEqual(first, second)
        self.upload.assert_awaited_once()
        self.persist.assert_awaited_once()
        self.emitter.assert_awaited_once()
        self.assertEqual(body["messages"], [{"role": "tool", "content": "generated"}])

    async def test_resume_uses_saved_attachment_without_reupload(self):
        self.message["files"] = [FILE]
        await self.run_request()
        self.upload.assert_not_awaited()

    async def test_incoming_body_cannot_authorize_file_reads(self):
        self.message["output"] = []
        await self.run_request({"messages": [{"role": "assistant", "output": output()}]})
        self.upload.assert_not_awaited()

    async def test_no_access_never_reads_or_uploads(self):
        self.owned.side_effect = ValueError("denied")
        body = {"messages": []}
        self.assertIs(await self.filter.request(body, USER, META, self.req), body)
        self.upload.assert_not_awaited()

    async def test_parallel_hooks_upload_only_once(self):
        await asyncio.gather(self.run_request(), self.run_request())
        self.upload.assert_awaited_once()

    async def test_failed_upload_preserves_original_response(self):
        self.upload.side_effect = OSError("unavailable")
        body = {"messages": [{"role": "tool", "content": "original URL and poster"}]}
        result = await self.run_request(body)
        self.assertEqual(result["messages"][0], body["messages"][0])
        self.assertIn(media.VIDEO_GUIDANCE, [m["content"] for m in result["messages"]])
        self.persist.assert_not_awaited()

    async def test_failed_persistence_retries_cached_upload(self):
        original = self.persist.side_effect
        self.persist.side_effect = ValueError("transient failure")
        body = {"messages": [{"role": "tool", "content": "original result"}]}
        result = await self.run_request(body)
        self.assertEqual(result["messages"][0], body["messages"][0])
        self.persist.side_effect = original
        await self.run_request(body)
        self.upload.assert_awaited_once()
        self.assertEqual(self.persist.await_count, 2)

    async def test_outlet_persists_late_result_but_never_adds_player_markup(self):
        self.message["files"] = [FILE]
        body = {"messages": [{"id": "answer", "role": "assistant",
                              "content": "Your clip.", "output": []}]}
        before = copy.deepcopy(body)
        # Presentation is now entirely the native display tool / download link.
        self.assertEqual(await self.filter.outlet(body, USER, META, self.req), body)
        self.assertEqual(body, before)
        self.persist.assert_not_awaited()  # already persisted in asyncSetUp flow

    async def test_outlet_failure_returns_original_body(self):
        self.owned.side_effect = ValueError("denied")
        body = {"messages": [{"id": "answer", "role": "assistant",
                              "content": "Your clip.", "output": []}]}
        self.assertIs(await self.filter.outlet(body, USER, META, self.req), body)

    async def test_notice_lists_only_undisplayed_attachments_for_native_display(self):
        body = {"messages": [{"role": "tool", "content": "generated"}]}
        fresh = await self.filter.request(body, USER, META, self.req, self.emitter)
        system = str(fresh["messages"][-1]["content"])
        self.assertIn("display_file", system)
        self.assertIn(FILE["url"], system)
        self.assertIn("Not yet displayed:\n- " + FILE["url"], system)
        self.message["output"] += display_output()
        repeated = await self.filter.request(body, USER, META, self.req, self.emitter)
        system = str(repeated["messages"][-1]["content"])
        self.assertIn("Not yet displayed:\nNone.", system)
        self.assertIn(FILE["url"], system)


class GuidanceTests(unittest.IsolatedAsyncioTestCase):
    def message(self, name="comfyui_generate_video"):
        items = output(name)
        items[1]["output"].append({"type": "input_image", "image_url": POSTER})
        return {"role": "assistant", "output": items}

    def body(self, text=media.SYNTHETIC_ANALYSIS, images=(POSTER,)):
        return {"metadata": {"live": object()}, "messages": [
            {"role": "tool", "tool_call_id": "call", "content": "Video generated."}, {
            "role": "user", "content": [
                {"type": "text", "text": text},
                *[{"type": "image_url", "image_url": {"url": url}} for url in images]]}]}

    async def test_verified_video_synthetic_prompt_becomes_neutral_and_keeps_poster(self):
        body = self.body()
        before = copy.deepcopy(body["messages"])
        result = await media.video_guidance(body, self.message(), USER)
        self.assertEqual(result["messages"][1]["content"][0]["text"], media.VIDEO_PREVIEW_CONTEXT)
        self.assertEqual(result["messages"][1]["content"][1], body["messages"][1]["content"][1])
        self.assertIs(result["metadata"], body["metadata"])
        self.assertEqual(body["messages"], before)
        self.assertEqual(result["messages"][-1]["content"], media.VIDEO_GUIDANCE)
        self.assertEqual(await media.video_guidance(result, self.message(), USER), result)

    async def test_explicit_user_analysis_request_unchanged(self):
        body = self.body(text="Please inspect the blades in this video.")
        result = await media.video_guidance(body, self.message(), USER)
        self.assertEqual(result["messages"][:2], body["messages"])

    async def test_mixed_unknown_or_unrelated_images_keep_their_instructions(self):
        for images in [(OTHER,), (POSTER, OTHER), ()]:
            body = self.body(images=images)
            result = await media.video_guidance(body, self.message(), USER)
            self.assertEqual(result["messages"][:2], body["messages"])

    async def test_identical_genuine_user_prompt_before_current_tool_is_not_rewritten(self):
        body = self.body()
        body["messages"].reverse()
        result = await media.video_guidance(body, self.message(), USER)
        self.assertEqual(result["messages"][:2], body["messages"])

    async def test_image_gif_unpaired_or_incomplete_results_do_not_authorize_rewriting(self):
        for name in ["comfyui_generate_image", "comfyui_generate_gif", "other"]:
            body = self.body()
            self.assertIs(await media.video_guidance(body, self.message(name), USER), body)
        for items in [self.message()["output"][1:], self.message()["output"]]:
            if len(items) == 2:
                items[1]["status"] = "in_progress"
            body = self.body()
            self.assertIs(await media.video_guidance(body, {"output": items}, USER), body)

    async def test_no_poster_still_adds_delivery_and_fetch_guidance(self):
        body = {"messages": []}
        result = await media.video_guidance(body, {"output": output()}, USER)
        self.assertEqual(result["messages"], [{"role": "system", "content": media.VIDEO_GUIDANCE}])

    async def test_guidance_and_attachment_notice_extend_existing_system_policy(self):
        for content in ["Original policy.", [{"type": "text", "text": "Original policy."}]]:
            body = self.body()
            body["messages"].insert(0, {"role": "system", "content": content})
            before = copy.deepcopy(body["messages"])
            guided = await media.video_guidance(body, self.message(), USER)
            result = media.attachment_notice(guided, [FILE])
            systems = [m for m in result["messages"] if m["role"] == "system"]
            self.assertEqual(len(systems), 1)
            self.assertIn("Original policy.", str(systems[0]["content"]))
            self.assertIn(media.VIDEO_GUIDANCE, str(systems[0]["content"]))
            self.assertIn(FILE["url"], str(systems[0]["content"]))
            self.assertEqual(body["messages"], before)
            self.assertEqual(media.attachment_notice(result, [FILE]), result)

if __name__ == "__main__":
    unittest.main()
