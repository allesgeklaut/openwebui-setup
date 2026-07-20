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


async def _put(path: str, params: dict = None, json: dict = None):
    async with httpx.AsyncClient(timeout=60, verify=False) as c:
        r = await c.put(f"{BASE}{path}", headers=HEADERS, params=params, json=json)
        r.raise_for_status()
        return r


async def _delete(path: str, params: dict = None):
    async with httpx.AsyncClient(timeout=30, verify=False) as c:
        r = await c.delete(f"{BASE}{path}", headers=HEADERS, params=params)
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


@mcp.tool()
async def remove_container(container_id: str, force: bool = False) -> str:
    """Remove a container by ID."""
    r = await _delete(
        f"/api/endpoints/{ENV_ID}/docker/containers/{container_id}",
        {"force": str(force).lower()},
    )
    return f"Removed (HTTP {r.status_code})"


# ---------- Images / ad-hoc container creation ----------

@mcp.tool()
async def pull_image(image: str, tag: str = "latest") -> str:
    """Pull a Docker image before creating a container from it."""
    r = await _post(
        f"/api/endpoints/{ENV_ID}/docker/images/create",
        {"fromImage": image, "tag": tag},
    )
    return f"Pulled {image}:{tag} (HTTP {r.status_code})"


@mcp.tool()
async def create_container(
    name: str,
    image: str,
    ports: dict = None,
    env: list = None,
    volumes: list = None,
    restart_policy: str = "unless-stopped",
) -> dict:
    """Create a new standalone container (not part of a compose stack).
    ports: {'8080/tcp': 8080}, env: ['KEY=VALUE'], volumes: ['/host:/container']."""
    port_bindings = {}
    exposed_ports = {}
    if ports:
        for container_port, host_port in ports.items():
            exposed_ports[container_port] = {}
            port_bindings[container_port] = [{"HostPort": str(host_port)}]

    body = {
        "Image": image,
        "Env": env or [],
        "ExposedPorts": exposed_ports,
        "HostConfig": {
            "PortBindings": port_bindings,
            "Binds": volumes or [],
            "RestartPolicy": {"Name": restart_policy},
        },
    }

    r = await _post(
        f"/api/endpoints/{ENV_ID}/docker/containers/create",
        {"name": name},
        body,
    )
    return r.json()


# ---------- Stacks (compose projects tracked by Portainer) ----------

@mcp.tool()
async def list_stacks() -> list:
    """List all Portainer-tracked stacks (compose projects deployed via Portainer)."""
    data = await _get("/api/stacks")
    return [{"id": s["Id"], "name": s["Name"], "status": s["Status"]} for s in data]


@mcp.tool()
async def create_stack(name: str, compose_content: str) -> dict:
    """Deploy a new stack from raw docker-compose YAML content. Becomes tracked by Portainer."""
    r = await _post(
        "/api/stacks/create/standalone/string",
        {"endpointId": ENV_ID},
        {"name": name, "stackFileContent": compose_content, "env": []},
    )
    return r.json()


@mcp.tool()
async def update_stack(stack_id: int, compose_content: str) -> dict:
    """Update and redeploy an existing Portainer-tracked stack with new compose content."""
    r = await _put(
        f"/api/stacks/{stack_id}",
        {"endpointId": ENV_ID},
        {"stackFileContent": compose_content, "env": [], "prune": False},
    )
    return r.json()


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


@mcp.tool()
async def delete_stack(stack_id: int) -> str:
    """Delete a Portainer-tracked stack and its containers entirely."""
    r = await _delete(f"/api/stacks/{stack_id}", {"endpointId": ENV_ID})
    return f"Deleted (HTTP {r.status_code})"


# ---------- Environments ----------

@mcp.tool()
async def list_environments() -> list:
    """List all Portainer-managed environments (Docker hosts)."""
    data = await _get("/api/endpoints")
    return [{"id": e["Id"], "name": e["Name"], "url": e["URL"]} for e in data]


if __name__ == "__main__":
    mcp.run(transport="stdio")

