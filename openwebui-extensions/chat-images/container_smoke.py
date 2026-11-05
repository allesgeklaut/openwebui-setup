"""Run synthetic tests against the installed OWUI runtime without activating code."""

import json
from pathlib import Path
import subprocess
import sys


RUNTIME = r'''
import asyncio
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

payload = json.load(sys.stdin)
original_stdout = sys.stdout
captured = io.StringIO()
try:
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        root = Path('/tmp/opencode')
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='chat-images-', dir=root) as folder:
            folder = Path(folder)
            for relative, text in payload.items():
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            sys.path.insert(0, str(folder))
            import chat_image_retrieval as extension
            import test_chat_images as fixtures
            suite = unittest.defaultTestLoader.loadTestsFromModule(fixtures)
            tests = unittest.TextTestRunner(stream=captured).run(suite)
            if not tests.wasSuccessful():
                raise AssertionError('Unit tests failed in container')

            from open_webui.utils import middleware
            from open_webui.utils.plugin import load_function_module_by_id, load_tool_module_by_id
            from open_webui.utils.tools import get_tool_specs
            from open_webui.models.chats import Chats
            from open_webui.models.files import Files

            async def smoke():
                function, kind, _, _ = await load_function_module_by_id(
                    'chat_images_synthetic_test', content=payload['filter.py'])
                tool, _, _ = await load_tool_module_by_id(
                    'chat_images_synthetic_test', content=payload['tool.py'])
                assert kind == 'filter' and hasattr(function, 'request')
                specs = get_tool_specs(tool)
                assert len(specs) == 1 and specs[0]['name'] == 'retrieve_chat_image'
                assert specs[0]['parameters']['required'] == ['image_ref']

                messages = {m['id']: m for m in fixtures.branch()}
                chat = SimpleNamespace(chat={'history': {'messages': messages}})
                with patch.object(Chats, 'get_chat_by_id_and_user_id', AsyncMock(return_value=chat)) as lookup:
                    catalog, latest = await extension.owned_context(fixtures.USER, fixtures.META)
                    lookup.assert_awaited_once_with('chat', 'owner')
                    assert 'first:1' in catalog and latest == [fixtures.NEW]
                with patch.object(Chats, 'get_chat_by_id_and_user_id', AsyncMock(return_value=chat)) as lookup:
                    assert await extension.response_images(fixtures.USER, fixtures.META) == {}
                    lookup.assert_awaited_once_with('chat', 'owner')
                with patch.object(Chats, 'get_chat_by_id_and_user_id', AsyncMock(return_value=None)):
                    try:
                        await extension.owned_context(fixtures.USER, fixtures.META)
                    except ValueError:
                        pass
                    else:
                        raise AssertionError('Non-owned chat accepted')

                with patch.object(Files, 'get_file_by_id_and_user_id', AsyncMock(return_value=None)) as lookup:
                    try:
                        await extension.load_source('/api/v1/files/file-test/content', 'owner')
                    except ValueError:
                        pass
                    else:
                        raise AssertionError('Non-owned file accepted')
                    lookup.assert_awaited_once_with('file-test', 'owner')

                req = fixtures.request()
                meta = {**fixtures.META, 'chat_id': '00000000-0000-4000-8000-000000000001'}
                response_snapshot = AsyncMock(return_value={})
                with patch.object(extension, 'owned_context', AsyncMock(return_value=fixtures.context())), \
                     patch.object(extension, 'response_images', response_snapshot):
                    await function.inlet({'messages': []}, fixtures.USER, meta, req)
                    image = await tool.retrieve_chat_image('first:1', fixtures.USER, meta,
                                                          req, AsyncMock(return_value=True))
                    assert image.startswith('data:image/jpeg;base64,')
                    text, files, embeds = await middleware.process_tool_result(
                        req, 'retrieve_chat_image', image, 'local', metadata=meta,
                        user=SimpleNamespace(id='owner'))
                    assert len(files) == 1 and files[0]['url'] == image
                    file_url = '/api/v1/files/synthetic-image/content'
                    user = SimpleNamespace(id='owner', role='user')
                    with patch.object(middleware, 'get_file_url_from_base64',
                                      AsyncMock(return_value=file_url)) as storage:
                        stored_url = await middleware.store_tool_result_image(req, image, meta, user)
                        storage.assert_awaited_once()
                        assert stored_url == file_url
                    image_path = folder / 'synthetic.jpg'
                    image_path.write_bytes(extension.decode_image_url(image))
                    record = SimpleNamespace(id='synthetic-image', user_id='owner',
                                             path=str(image_path), meta={'content_type': 'image/jpeg'})
                    output = [
                        {'type': 'function_call', 'id': 'fc', 'call_id': 'call',
                         'name': 'retrieve_chat_image', 'arguments': '{"image_ref":"first:1"}',
                         'status': 'completed'},
                        {'type': 'function_call_output', 'id': 'fco', 'call_id': 'call',
                         'status': 'completed', 'output': [
                             {'type': 'input_text', 'text': text},
                             {'type': 'input_image', 'image_url': stored_url}]},
                    ]
                    converted = middleware.process_messages_with_output([
                        {'role': 'assistant', 'output': output}])
                    body = {'messages': converted}
                    with patch.object(Files, 'get_file_by_id', AsyncMock(return_value=record)) as lookup:
                        body = await middleware.convert_url_images_to_base64(body, user=user)
                        lookup.assert_awaited_once_with('synthetic-image')
                    approved = await function.request(body, fixtures.USER, meta, req)
                    assert image in json.dumps(approved), 'Approved image lost in tool conversion'
                    next_req = fixtures.request()
                    later = await function.inlet(body, fixtures.USER, meta, next_req)
                    assert image not in json.dumps(later), 'Historical retrieval leaked into next request'

                    # Exercise the real builtin's JSON + UI files path, without
                    # running a generator, calling a provider or writing live DB rows.
                    from open_webui.tools import builtin
                    from open_webui import config
                    generated_url = '/api/v1/files/generated-test/content'
                    generated_files = [{'type': 'image', 'url': generated_url}]
                    generated_raw = fixtures.png('green')
                    generated_uri = fixtures.uri(generated_raw)
                    uploads = folder / 'uploads'
                    uploads.mkdir()
                    generated_path = uploads / 'generated.png'
                    generated_path.write_bytes(generated_raw)
                    generated_record = SimpleNamespace(id='generated-test', user_id='owner',
                                                       path=str(generated_path), meta={})
                    emitter = AsyncMock()
                    with patch.object(builtin, 'image_generations', AsyncMock(return_value=[
                            {'url': generated_url}])) as generator, \
                         patch.object(Chats, 'add_message_files_by_id_and_message_id',
                                      AsyncMock(return_value=generated_files)):
                        generated_text = await builtin.generate_image(
                            'Synthetic wiring test; generator is mocked', __request__=req,
                            __chat_id__=meta['chat_id'], __message_id__=meta['message_id'],
                            __event_emitter__=emitter)
                        generator.assert_awaited_once()
                    assert json.loads(generated_text)['status'] == 'success'
                    assert emitter.await_args.args[0]['data']['files'] == generated_files
                    response_snapshot.return_value = {'call:generate': (generated_url,)}
                    generated_body = {'messages': [
                        {'role': 'tool', 'tool_call_id': 'generate', 'content': generated_text},
                        {'role': 'user', 'content': [fixtures.part(generated_uri)]}]}
                    with patch.object(config, 'UPLOAD_DIR', uploads), \
                         patch.object(Files, 'get_file_by_id_and_user_id',
                                      AsyncMock(return_value=generated_record)) as lookup:
                        kept = await function.request(generated_body, fixtures.USER, meta, req)
                        lookup.assert_awaited_once_with('generated-test', 'owner')
                    assert kept == generated_body, 'Current image generation output was dropped'
                    assert emitter.await_args.args[0]['data']['files'] == generated_files

            asyncio.run(smoke())
            result = {'unit_tests': tests.testsRun, 'native_loaders': 'pass',
                      'ownership_adapters': 'pass', 'tool_image_roundtrip': 'pass',
                      'next_request_pruning': 'pass', 'provider_calls': 0,
                      'native_image_generation_output': 'pass',
                      'registered_extensions': 0}
    original_stdout.write(json.dumps(result) + '\n')
except Exception as exc:
    # Do not print runtime initialization logs/configuration or image payloads.
    original_stdout.write(json.dumps({'status': 'failed', 'error_type': type(exc).__name__,
                                     'error': str(exc) if isinstance(exc, AssertionError) else
                                     'Runtime smoke test failed; inspect locally without logging secrets.'}) + '\n')
    sys.exit(1)
'''


def main():
    root = Path(__file__).resolve().parent
    files = {
        relative: (root / relative).read_text()
        for relative in ["chat_image_retrieval/__init__.py", "filter.py", "tool.py", "test_chat_images.py"]
    }
    result = subprocess.run(
        ["docker", "exec", "-i", "-w", "/app/backend", "open-webui", "python", "-c", RUNTIME],
        input=json.dumps(files), text=True, capture_output=True, timeout=120,
    )
    print(result.stdout.strip() or "Container smoke test failed without a result.")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
