import os
import httpx
from fastmcp import FastMCP

mcp = FastMCP("portainer")

BASE = os.environ["PORTAINER_URL"]
API_KEY = os.environ["PORTAINER_API_KEY"]
ENV_ID = os.environ.get("PORTAINER_ENV_ID", "3")
HEADERS = {"X-API-Key": API_KEY}


async def _get(path: str, params: dict = None):
    async with httpx.AsyncClient(timeout=15, verify=False) as c:
        r = await c.get(f"{BASE}{path}", headers=HEADERS, params=params)
        r.raise_for_status()
        return r.json()


async def _post(path: str, params: dict = None, json: dict = None):
    async with httpx.AsyncClient(timeout=60, verify=False) as c:
        r = await c.post(f"{BASE}{path}", headers=HEADERS, params=params, json=json)
        r.raise_for_status()
        return r


# ---------- Containers ----------

@mcp.tool()
async def list_containers(all: bool = True) -> list:
    """List Docker containers on the managed environment."""
    data = await _get(f"/api/endpoints/{ENV_ID}/docker/containers/json", {"all": str(all).lower()})
    return [
        {
            "id": c["Id"][:12],
            "name": c["Names"][0].lstrip("/"),
            "state": c["State"],
            "status": c["Status"],
        }
        for c in data
    ]


@mcp.tool()
async def container_info(container_id: str) -> dict:
    """Get detailed info about a specific container."""
    return await _get(f"/api/endpoints/{ENV_ID}/docker/containers/{container_id}/json")


@mcp.tool()
async def start_container(container_id: str) -> str:
    """Start a stopped container."""
    r = await _post(f"/api/endpoints/{ENV_ID}/docker/containers/{container_id}/start")
    return f"Started (HTTP {r.status_code})"


@mcp.tool()
async def stop_container(container_id: str) -> str:
    """Stop a running container."""
    r = await _post(f"/api/endpoints/{ENV_ID}/docker/containers/{container_id}/stop")
    return f"Stopped (HTTP {r.status_code})"


@mcp.tool()
async def restart_container(container_id: str) -> str:
    """Restart a container."""
    r = await _post(f"/api/endpoints/{ENV_ID}/docker/containers/{container_id}/restart")
    return f"Restarted (HTTP {r.status_code})"


# ---------- Stacks (compose projects tracked by Portainer) ----------

@mcp.tool()
async def list_stacks() -> list:
    """List all Portainer-tracked stacks (compose projects deployed via Portainer)."""
    data = await _get("/api/stacks")
    return [{"id": s["Id"], "name": s["Name"], "status": s["Status"]} for s in data]


@mcp.tool()
async def stack_up(stack_id: int) -> str:
    """Start (docker compose up) a Portainer-tracked stack by ID."""
    r = await _post(f"/api/stacks/{stack_id}/start", {"endpointId": ENV_ID})
    return f"Stack {stack_id} started (HTTP {r.status_code})"


@mcp.tool()
async def stack_down(stack_id: int) -> str:
    """Stop (docker compose down) a Portainer-tracked stack by ID."""
    r = await _post(f"/api/stacks/{stack_id}/stop", {"endpointId": ENV_ID})
    return f"Stack {stack_id} stopped (HTTP {r.status_code})"


if __name__ == "__main__":
    mcp.run(transport="stdio")

