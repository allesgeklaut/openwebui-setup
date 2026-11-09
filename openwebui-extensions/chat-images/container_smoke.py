"""Run synthetic tests against the installed OWUI runtime without activating code."""

import argparse
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
installed = payload.pop('_installed', False)
original_stdout = sys.stdout
captured = io.StringIO()
try:
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        root = Path('/tmp/opencode')
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='chat-images-', dir=root) as folder:
            folder = Path(folder)
            for relative, text in payload.items():
                if installed and relative == 'chat_image_retrieval/__init__.py':
                    continue
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            sys.path.insert(0, str(folder))
            import chat_image_retrieval as extension
            if installed:
                assert Path(extension.__file__).resolve() == Path(
                    '/app/backend/extensions/chat_image_retrieval/__init__.py')
                assert not Path('/app/backend/chat_image_retrieval').exists(), 'Old package mount remains'
            import test_chat_images as fixtures
            suite = unittest.defaultTestLoader.loadTestsFromModule(fixtures)
            tests = unittest.TextTestRunner(stream=captured).run(suite)
            if not tests.wasSuccessful():
                raise AssertionError('Unit tests failed in container')

            from open_webui.utils import middleware
            from open_webui.utils.plugin import (
                extract_frontmatter, load_function_module_by_id, load_tool_module_by_id)
            from open_webui.utils.tools import get_tool_specs
            from open_webui.models.chats import Chats
            from open_webui.models.files import Files

            async def smoke():
                filter_code, tool_code = payload['filter.py'], payload['tool.py']
                if installed:
                    from open_webui.models.functions import Functions
                    from open_webui.models.tools import Tools
                    registered_filter = await Functions.get_function_by_id('chat_image_retrieval_filter')
                    registered_tool = await Tools.get_tool_by_id('chat_image_retrieval')
                    assert registered_filter and registered_filter.is_active
                    assert registered_tool
                    assert registered_filter.content == filter_code, 'Registered filter differs from repo'
                    assert registered_tool.content == tool_code, 'Registered tool differs from repo'
                    assert registered_filter.meta.manifest == extract_frontmatter(filter_code)
                    assert registered_tool.meta.manifest == extract_frontmatter(tool_code)
                    filter_code, tool_code = registered_filter.content, registered_tool.content
                function, kind, _, _ = await load_function_module_by_id(
                    'chat_images_synthetic_test', content=filter_code)
                tool, _, _ = await load_tool_module_by_id(
                    'chat_images_synthetic_test', content=tool_code)
                assert kind == 'filter' and hasattr(function, 'request')
                assert type(function) is extension.Filter and type(tool) is extension.Tools
                assert function.valves.gif_max_frames == 4
                assert function.valves.max_expanded_images == 16
                schema = function.Valves.model_json_schema()['properties']
                assert schema['gif_max_frames']['default'] == 4
                assert schema['gif_max_frames']['minimum'] == 1
                assert schema['gif_max_frames']['maximum'] == 4
                assert 'gif_first_frame_models' not in schema
                assert function.Valves(gif_first_frame_models='mistral').gif_max_frames == 4
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
                    for source, expected_id in [('/api/v1/files/file-test/content', 'file-test'),
                                                (fixtures.FILE_ID, fixtures.FILE_ID)]:
                        lookup.reset_mock()
                        try:
                            await extension.load_source(source, 'owner')
                        except ValueError:
                            pass
                        else:
                            raise AssertionError('Non-owned file accepted')
                        lookup.assert_awaited_once_with(expected_id, 'owner')

                # Native URL resolution runs before inlet: saved chat metadata
                # keeps the URL while provider content contains base64 bytes.
                from open_webui import config
                gif_path = folder / 'synthetic.gif'
                gif_bytes = fixtures.gif(6, duration=100)
                gif_path.write_bytes(gif_bytes)
                gif_record = SimpleNamespace(id=fixtures.FILE_ID, user_id='owner',
                                             path=str(gif_path), meta={'content_type': 'image/gif'})
                for gif_url, model in [
                        (source, model) for source in [f'/api/v1/files/{fixtures.FILE_ID}/content', fixtures.FILE_ID]
                        for model in ['synthetic-mistral', 'synthetic-vision-model']]:
                    gif_body = {'model': model, 'messages': [
                        {'role': 'user', 'content': [fixtures.part(gif_url)],
                         'files': [{'type': 'file', 'content_type': 'image/gif',
                                     'id': fixtures.FILE_ID, 'url': gif_url,
                                     'name': 'synthetic-animation.gif',
                                     'file': {'id': fixtures.FILE_ID, 'meta': {'content_type': 'image/gif'}}}]}]}
                    with patch.object(Files, 'get_file_by_id', AsyncMock(return_value=gif_record)):
                        resolved = await middleware.convert_url_images_to_base64(
                            gif_body, user=SimpleNamespace(id='owner', role='user'))
                    assert resolved['messages'][0]['content'][0]['image_url']['url'].startswith('data:')
                    with patch.object(config, 'UPLOAD_DIR', folder), \
                         patch.object(Files, 'get_file_by_id_and_user_id', AsyncMock(return_value=gif_record)) as lookup, \
                         patch.object(extension, 'owned_context', AsyncMock(return_value=({}, [gif_url]))), \
                         patch.object(extension, 'response_images', AsyncMock(return_value={})):
                        gif_req = fixtures.request()
                        framed = await function.inlet(resolved, fixtures.USER, fixtures.META, gif_req)
                        lookup.assert_awaited_once_with(fixtures.FILE_ID, 'owner')
                        frame = framed['messages'][0]['content'][0]['image_url']['url']
                        assert frame.startswith('data:image/jpeg;base64,')
                        assert framed['messages'][0]['files'][0]['url'] == frame
                        assert await function.request(resolved, fixtures.USER, fixtures.META, gif_req) == framed
                        # Exercise native file-context formatting and tool-loop
                        # reconstruction, not only our intermediate files list.
                        converted = middleware.process_messages_with_output(
                            framed['messages'], include_file_context=True)
                        provider_body = await function.request(
                            {'model': gif_body['model'], 'messages': converted},
                            fixtures.USER, fixtures.META, gif_req)
                        serialized = json.dumps(provider_body)
                        assert fixtures.FILE_ID not in serialized, 'Original GIF id exposed'
                        assert 'image/gif' not in serialized, 'Original GIF MIME exposed'
                        assert '<attached_files>' not in serialized, 'GIF advertised as inspectable file'
                        assert serialized.count(extension.GIF_SAMPLING_NOTICE) == 1
                        assert 'synthetic-animation.gif' not in serialized
                        image_parts = [item for item in provider_body['messages'][0]['content']
                                       if item.get('type') == 'image_url']
                        assert len(image_parts) == 4, 'GIF did not expand to four images'
                        group = extension.request_state(gif_req, fixtures.USER, fixtures.META)['substitutions'][gif_url]
                        assert [sample['timestamp_ms'] for sample in group['frames']] == [0, 200, 300, 500]
                        assert frame in serialized
                        assert await function.request(
                            provider_body, fixtures.USER, fixtures.META, gif_req) == provider_body
                        # Fresh native tool images can also use bare file IDs.
                        keys = await extension.response_source_keys({'call:generate': (gif_url,)}, 'owner')
                        assert extension.raw_fingerprint(gif_bytes) in keys

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
                      'native_gif_upload_roundtrip': 'pass',
                      'native_bare_file_id': 'pass',
                      'native_gif_disclosure_and_file_context': 'pass',
                      'native_four_frame_sampling_all_models': 'pass',
                      'installed_entries': 'pass' if installed else 'not_checked',
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', action='store_true',
                        help='Verify the mounted package and read registered filter/tool code; no DB writes.')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    files = {
        relative: (root / relative).read_text()
        for relative in ["filter.py", "tool.py", "test_chat_images.py"]
    }
    files['chat_image_retrieval/__init__.py'] = (
        root.parent / 'chat_image_retrieval' / '__init__.py').read_text()
    files['_installed'] = args.installed
    result = subprocess.run(
        ["docker", "exec", "-i", "-w", "/app/backend", "open-webui", "python", "-c", RUNTIME],
        input=json.dumps(files), text=True, capture_output=True, timeout=120,
    )
    print(result.stdout.strip() or "Container smoke test failed without a result.")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
