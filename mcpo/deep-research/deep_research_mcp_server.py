"""Deep Research MCP server: fronts the "Deep Researcher" Mistral agent.

Exposes one tool, deep_research(question), which runs a full agentic web
research turn (the configured Studio agent model + web_search_premium, multiple refined
searches, page opens) and returns the structured report with numbered
markdown citations.

Wired into:
  * mcpo (Open WebUI's MCP gateway) — via template_config.json
  * opencode — via the mcp section of /opt/stacks/opencode.json

Secrets: reads MISTRAL_API_KEY from the environment, falling back to
MISTRAL_API_KEY_FILE (an env-style KEY=VALUE file, default
/opt/secrets/mistral.env — the single source of truth also used by the
litellm stack). Nothing secret lives in this repo.
"""

import os

import httpx
from fastmcp import FastMCP

MISTRAL_API_URL = os.environ.get("MISTRAL_API_URL", "https://api.mistral.ai/v1/conversations")
# Env-style file (KEY=VALUE lines) backing both the MISTRAL_API_KEY and the
# DEEP_RESEARCH_AGENT_ID lookups; either value may also come from the env.
CONFIG_FILE = os.environ.get("MISTRAL_API_KEY_FILE", "/opt/secrets/mistral.env")
# Deep research can legitimately take minutes (5+ searches + page opens).
TIMEOUT_SECONDS = float(os.environ.get("DEEP_RESEARCH_TIMEOUT", 300))


def _read_config_value(name: str) -> str:
    direct = (os.environ.get(name) or "").strip()
    if direct:
        return direct
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                if "=" in line:
                    key, _, val = line.partition("=")
                    if key.strip() == name:
                        return val.strip().strip("'\"")
    return ""


def _load_mistral_key() -> str:
    key = _read_config_value("MISTRAL_API_KEY")
    if not key:
        raise RuntimeError(
            f"MISTRAL_API_KEY not set and no MISTRAL_API_KEY line found in {CONFIG_FILE}"
        )
    return key


MISTRAL_API_KEY = _load_mistral_key()
DEEP_RESEARCH_AGENT_ID = _read_config_value("DEEP_RESEARCH_AGENT_ID")
if not DEEP_RESEARCH_AGENT_ID:
    raise RuntimeError(
        "DEEP_RESEARCH_AGENT_ID not set and no DEEP_RESEARCH_AGENT_ID line "
        f"found in {CONFIG_FILE}"
    )


mcp = FastMCP("deep-research")


def _render_report(conv: dict) -> str:
    """Answer text with inline citation markers + a numbered sources list."""
    answer_parts: list[str] = []
    sources: list[dict] = []

    for e in conv.get("outputs", []):
        if e.get("type") != "message.output":
            continue
        for chunk in e.get("content", []):
            if not isinstance(chunk, dict):
                continue
            if chunk.get("type") == "text" and chunk.get("text"):
                answer_parts.append(chunk["text"])
            elif chunk.get("type") == "tool_reference" and chunk.get("url"):
                sources.append(
                    {"title": chunk.get("title") or chunk["url"], "url": chunk["url"]}
                )
                # inline marker at the position where the model cited it
                answer_parts.append(f"[{len(sources)}]")

    report = "".join(answer_parts).strip()
    if not report:
        report = "(no answer content returned)"

    if sources:
        seen: set[str] = set()
        lines = [
            f"{i}. [{s['title']}]({s['url']})"
            for i, s in enumerate(sources, start=1)
            if not (s["url"] in seen or seen.add(s["url"]))
        ]
        report += "\n\n## Sources\n" + "\n".join(lines)

    usage = conv.get("usage") or {}
    searches = sum(
        1
        for e in conv.get("outputs", [])
        if isinstance(e, dict) and e.get("type") == "tool.execution"
    )
    footer = f"\n\n---\n*{searches} web searches"
    if usage.get("completion_tokens"):
        footer += f", {usage['completion_tokens']} tokens"
    footer += "*"
    return report + footer


@mcp.tool()
async def deep_research(question: str) -> str:
    """Run deep multi-source web research on a question and return a
    structured report with citations.

    Use for open-ended, multi-faceted, or time-sensitive questions where
    thoroughness matters more than speed (takes 30-120 seconds and costs
    roughly $0.10-0.30 per call). Not for quick facts.

    Args:
        question: the research question, in any language. The report comes
            back in the same language as the question.
    """
    payload = {
        "agent_id": DEEP_RESEARCH_AGENT_ID,
        "store": False,
        "inputs": [{"role": "user", "content": question}],
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        resp = await client.post(
            MISTRAL_API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"},
        )
        if resp.status_code != 200:
            raise ValueError(f"Mistral API HTTP {resp.status_code}")
        conv = resp.json()
    return _render_report(conv)


if __name__ == "__main__":
    mcp.run(transport="stdio")
