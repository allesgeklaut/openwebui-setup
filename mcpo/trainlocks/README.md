# Trainlocks MCP Server

A Model Context Protocol (MCP) server that gives Open WebUI models access to the
self-hosted [Training Log](../../trainlocks) app (strength training tracker),
both for reading and for logging workouts.

## Tools

- **`list_exercises`** — list all exercises
- **`list_templates`** — list session templates with their exercises
- **`list_sessions`** — recent workout sessions, newest first
- **`get_session`** — full details of one session, including all sets
- **`get_progression`** — per-session top weight / volume / reps for an exercise
- **`create_exercise`** — create a new exercise
- **`create_template`** — create a new empty template
- **`add_exercise_to_template`** — add an exercise to a template
- **`log_session`** — log a completed workout (reps and weight per set)
- **`edit_session_set`** — change one set in an existing session
- **`delete_session`** — delete a session

## Requirements

The Training Log app must expose its JSON API (endpoints under `/api/`), which
requires the `feature/mcp-json-api` branch of the trainlocks app to be running.

## Setup

1. Credentials live in `/opt/secrets/trainlocks.env` (also the app's env file):

    ```
    SECRET_KEY=...
    TRAINLOGS_USERNAME=...
    TRAINLOGS_PASSWORD=...
    ```

2. The `mcpo` container must reach the app by container name — both compose
   projects attach to the shared `stacks-shared` docker network
   (`TRAINLOGS_URL=http://training-log:8000`).

3. The server is registered in the MCPo config (`mcpServers.trainlocks`), run
   via `uvx --with fastmcp --with httpx python /stacks/webui/mcpo/trainlocks/trainlocks_mcp_server.py`.

4. Restart MCPo after changes: `docker compose -f /opt/stacks/webui/compose.yml restart mcpo`

## Auth

The server logs in against the app's own session-based auth
(`POST /login`, cookie `tl_session`) using the credentials above, keeps the
session in memory, and re-logs in automatically when the session expires
(the app answers unauthenticated API calls with a JSON 401 and unauthenticated
page requests with a 303 redirect to `/login`).
The cookie expires after 7 days (`SESSION_MAX_AGE`), so the server re-authenticates
on its own.

## Usage in Open WebUI

Example prompts:

- *"Show me my last 5 training sessions"*
- *"How is my bench press progressing?"*
- *"Log today's workout: bench press 5 sets of 5 reps at 80 kg, rows 4 sets of 8 reps at 60 kg"*
- *"Fix the first set of yesterday's session to 4 reps"*

## Notes

- Exercise and template references accept names (case-insensitive) or numeric ids.
- The server is stateless between requests apart from the login cookie; it opens
  a fresh HTTP client process per MCPo spawn.
- No tool touches data outside the training log app.
