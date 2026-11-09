"""Run with docker exec -i -e OPENCODE_SMOKE_MODEL=mistral/mistral-small-latest
open-terminal python - < opencode/smoke_test.py (on one shell line).

Uses an explicitly chosen authenticated Mistral model (API usage applies) and
synthetic text only. Creates/removes its own temporary
folder. Any tool permission request is rejected, never silently approved.
This is a manual, network-dependent direct-ACP integration test, not a WebUI
end-to-end test. Never run it automatically during install or container startup.
"""

import asyncio
import contextlib
import json
import os
import pathlib
import tempfile

REQUEST_TIMEOUT = 120
TEST_TIMEOUT = 240
CLEANUP_TIMEOUT = 10


async def main():
    selected_model = os.environ.get("OPENCODE_SMOKE_MODEL", "")
    if not selected_model.startswith("mistral/") or selected_model == "mistral/":
        raise RuntimeError("Set OPENCODE_SMOKE_MODEL to a Mistral model; API usage applies")
    with tempfile.TemporaryDirectory(prefix=".opencode-smoke-", dir="/home/user") as work:
        process = await asyncio.create_subprocess_exec(
            "/home/user/.local/bin/opencode", "acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=work,
        )
        sequence = 0
        text = []
        rejected_permissions = []

        async def send(message):
            process.stdin.write((json.dumps(message) + "\n").encode())
            await process.stdin.drain()

        async def request(method, params):
            # One deadline for the whole request, including continuous updates.
            async with asyncio.timeout(REQUEST_TIMEOUT):
                return await exchange(method, params)

        async def exchange(method, params):
            nonlocal sequence
            sequence += 1
            request_id = sequence
            await send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            while True:
                line = await process.stdout.readline()
                if not line:
                    raise RuntimeError("ACP process ended")
                message = json.loads(line)
                if message.get("method") == "session/request_permission":
                    rejected_permissions.append(message["params"].get("toolCall", {}).get("kind"))
                    option = next(
                        o for o in message["params"]["options"] if o["kind"] == "reject_once"
                    )
                    await send({
                        "jsonrpc": "2.0", "id": message["id"],
                        "result": {"outcome": {"outcome": "selected", "optionId": option["optionId"]}},
                    })
                elif message.get("method") == "session/update":
                    update = message.get("params", {}).get("update", {})
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        text.append(update.get("content", {}).get("text", ""))
                elif message.get("id") == request_id and "method" not in message:
                    if "error" in message:
                        raise RuntimeError(str(message["error"]))
                    return message.get("result", {})

        session = None
        initialized = {}
        try:
            initialized = await request("initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "terminal-smoke-test", "version": "1"},
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False,
                },
            })
            session = await request("session/new", {"cwd": work, "mcpServers": []})
            model = next(c for c in session["configOptions"] if c["category"] == "model")
            if model["currentValue"] != selected_model:
                await request("session/set_config_option", {
                    "sessionId": session["sessionId"], "configId": model["id"],
                    "value": selected_model,
                })
            result = await request("session/prompt", {
                "sessionId": session["sessionId"],
                "prompt": [{"type": "text", "text": (
                    "This is an isolated synthetic smoke test. Use your file edit/write tool "
                    "to create hello.txt in the current folder containing exactly "
                    "OPENCODE_SANDBOX_OK followed by a newline. Do not read other files "
                    "or use the shell. Then reply DONE."
                )}],
            })
            target = pathlib.Path(work) / "hello.txt"
            verified = target.exists() and target.read_text() == "OPENCODE_SANDBOX_OK\n"
            print("ACP version:", initialized.get("protocolVersion"))
            print("Prompt stop reason:", result.get("stopReason"))
            print("File edit verified:", verified)
            print("Agent response:", "".join(text)[-500:])
            if not verified:
                raise RuntimeError("File edit smoke test failed")
            await request("session/prompt", {
                "sessionId": session["sessionId"],
                "prompt": [{"type": "text", "text": (
                    "Now test permission rejection. Use your shell tool to run exactly "
                    "printf OPENCODE_APPROVAL_TEST. This command should require approval "
                    "and the test client will reject it. If rejected, report DENIED and "
                    "stop immediately; do not retry or use another tool."
                )}],
            })
            print("Rejected approval requests:", rejected_permissions)
            if "execute" not in rejected_permissions:
                raise RuntimeError("Expected a shell permission request")
        finally:
            capabilities = initialized.get("agentCapabilities", {}).get("sessionCapabilities", {})
            try:
                if session and "delete" in capabilities:
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await asyncio.wait_for(
                            request("session/delete", {"sessionId": session["sessionId"]}),
                            CLEANUP_TIMEOUT,
                        )
            finally:
                if process.returncode is None:
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()


if __name__ == "__main__":
    async def bounded_main():
        async with asyncio.timeout(TEST_TIMEOUT):
            await main()

    asyncio.run(bounded_main())
