import datetime
import os
from typing import Optional, Union

import httpx
from fastmcp import FastMCP
from pydantic import BaseModel

BASE = os.environ.get("TRAINLOGS_URL", "http://training-log:8000")
USERNAME = os.environ["TRAINLOGS_USERNAME"]
PASSWORD = os.environ["TRAINLOGS_PASSWORD"]

mcp = FastMCP("trainlocks")

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=BASE, timeout=30, follow_redirects=False
        )
    return _client


async def _login() -> None:
    c = _get_client()
    await c.post("/login", data={"username": USERNAME, "password": PASSWORD})
    if "tl_session" not in c.cookies:
        raise RuntimeError(
            "trainlocks login failed — check TRAINLOGS_USERNAME/TRAINLOGS_PASSWORD"
        )


async def _authed(method: str, path: str, **kwargs) -> httpx.Response:
    """Make a request, re-logging in if the session has expired.

    The app answers unauthenticated API requests with a JSON 401 and
    unauthenticated page requests with a 303 redirect to /login; either
    triggers a re-login + retry.
    """
    c = _get_client()
    r = await c.request(method, path, **kwargs)
    expired = (
        r.status_code == 401
        or r.status_code == 303
        and r.headers.get("location", "").startswith("/login")
    )
    if expired:
        await _login()
        r = await c.request(method, path, **kwargs)
    return r


def _fail(r: httpx.Response, what: str) -> str:
    try:
        return f"{what}: {r.json().get('detail', r.status_code)}"
    except ValueError:
        return f"{what}: HTTP {r.status_code}"


# ---------- Resolvers (accept names or numeric ids) ----------

async def _exercises() -> list:
    r = await _authed("GET", "/api/exercises")
    r.raise_for_status()
    return r.json()


async def _templates() -> list:
    r = await _authed("GET", "/api/templates")
    r.raise_for_status()
    return r.json()


async def _resolve_exercise(name: str) -> int:
    name = name.strip()
    if name.isdigit():
        return int(name)
    for e in await _exercises():
        if e["name"].lower() == name.lower():
            return e["id"]
    raise ValueError(f"exercise not found: {name!r}")


async def _resolve_template(name: str) -> int:
    name = name.strip()
    if name.isdigit():
        return int(name)
    for t in await _templates():
        if t["name"].lower() == name.lower():
            return t["id"]
    raise ValueError(
        f"template not found: {name!r} (available: "
        f"{', '.join(t['name'] for t in await _templates())})"
    )


# ---------- Reads ----------

@mcp.tool()
async def list_exercises() -> list:
    """List all exercises in the training log."""
    r = await _authed("GET", "/api/exercises")
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def list_templates() -> list:
    """List all session templates with their ordered exercises and set counts."""
    r = await _authed("GET", "/api/templates")
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def list_sessions(limit: int = 10) -> list:
    """List recent workout sessions, newest first.

    Args:
        limit: max number of sessions to return (default 10).
    """
    r = await _authed("GET", "/api/sessions", params={"limit": limit})
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def get_session(session_id: int) -> dict:
    """Get full details of one workout session, including every set.

    Args:
        session_id: id of the session (see list_sessions).
    """
    r = await _authed("GET", f"/api/sessions/{session_id}")
    if r.status_code == 404:
        raise ValueError("session not found")
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def get_progression(exercise: str) -> list:
    """Per-session progression data for one exercise: top weight, total volume
    and total reps per session, ordered by date.

    Args:
        exercise: exercise name (case-insensitive) or numeric id.
    """
    ex_id = await _resolve_exercise(exercise)
    r = await _authed("GET", f"/api/progression/{ex_id}")
    r.raise_for_status()
    return r.json()["data"]


# ---------- Writes ----------

@mcp.tool()
async def create_exercise(name: str, is_bodyweight: bool = False) -> str:
    """Create a new exercise.

    Args:
        name: exercise name.
        is_bodyweight: set true for bodyweight exercises (no barbell/dumbbell weight).
    """
    r = await _authed(
        "POST", "/exercises",
        data={"name": name.strip(), "is_bodyweight": "1" if is_bodyweight else "0"},
    )
    if r.status_code != 303:
        raise ValueError(_fail(r, "could not create exercise"))
    return f"Created exercise {name.strip()!r}"


@mcp.tool()
async def create_template(name: str) -> str:
    """Create a new empty session template.

    Args:
        name: template name.
    """
    r = await _authed("POST", "/templates", data={"name": name.strip()})
    if r.status_code != 303:
        raise ValueError(_fail(r, "could not create template"))
    return f"Created template {name.strip()!r}"


@mcp.tool()
async def add_exercise_to_template(template: str, exercise: str, sets: int) -> str:
    """Add an exercise to a session template.

    Args:
        template: template name or id.
        exercise: exercise name or id.
        sets: number of sets prescribed by the template.
    """
    tpl_id = await _resolve_template(template)
    ex_id = await _resolve_exercise(exercise)
    r = await _authed(
        "POST", f"/templates/{tpl_id}/add_exercise",
        data={"exercise_id": ex_id, "sets": sets},
    )
    if r.status_code == 404:
        raise ValueError(f"template not found: {template!r}")
    if r.status_code != 303:
        raise ValueError(_fail(r, "could not add exercise to template"))
    return f"Added {exercise!r} ({sets} sets) to template {template!r}"


class LoggedSet(BaseModel):
    reps: int
    weight: Optional[float] = None


class LoggedExercise(BaseModel):
    exercise: str
    sets: list[LoggedSet]


@mcp.tool()
async def log_session(
    exercises: list[LoggedExercise],
    template: Optional[str] = None,
    notes: Optional[str] = None,
    date: Optional[str] = None,
) -> str:
    """Log a completed workout session.

    Args:
        exercises: what was done, as a list of {exercise, sets}, where sets is a
            list of {reps, weight?} per set (weight in kg, omit for bodyweight).
        template: template name or id to associate with this session (optional).
        notes: optional session notes.
        date: workout date as YYYY-MM-DD (defaults to today).
    """
    workout_date = date or datetime.date.today().isoformat()
    form: dict = {"date": workout_date, "template_id": "", "notes": notes or ""}
    if template:
        form["template_id"] = str(await _resolve_template(template))

    seen: set[str] = set()
    for ex in exercises:
        key = ex.exercise.strip().lower()
        if key in seen:
            raise ValueError(f"duplicate exercise in one session: {ex.exercise!r}")
        seen.add(key)
        ex_id = await _resolve_exercise(ex.exercise)
        for i, s in enumerate(ex.sets, start=1):
            form[f"reps-{ex_id}-{i}"] = str(s.reps)
            if s.weight is not None:
                form[f"weight-{ex_id}-{i}"] = str(s.weight)

    r = await _authed(
        "POST", "/sessions/new", data=form,
        headers={"Accept": "application/json"},
    )
    if r.status_code != 201:
        raise ValueError(_fail(r, "could not log session"))
    session_id = r.json()["id"]
    summary = ", ".join(f"{e.exercise} ({len(e.sets)} sets)" for e in exercises)
    return f"Logged session {session_id} on {workout_date}: {summary}"


@mcp.tool()
async def edit_session_set(
    session_id: int,
    exercise: str,
    set_number: int,
    reps: int,
    weight: Optional[float] = None,
) -> str:
    """Change one set in an existing session (create it if it does not exist yet).

    Args:
        session_id: id of the session to edit.
        exercise: exercise name or id.
        set_number: set number to update.
        reps: new rep count. Set to 0 together with weight=None to delete the set.
        weight: new weight in kg. Omit to keep the set's existing weight (or
            leave it unset for a new bodyweight set).
    """
    r = await _authed("GET", f"/api/sessions/{session_id}")
    if r.status_code == 404:
        raise ValueError("session not found")
    r.raise_for_status()
    sess = r.json()

    ex_id = await _resolve_exercise(exercise)
    sets = {(s["exercise_id"], s["set_number"]): s for s in sess["sets"]}
    key = (ex_id, set_number)

    if reps <= 0 and weight is None:
        sets.pop(key, None)
    else:
        existing = sets.get(key)
        if weight is None and existing is not None:
            weight = existing["weight"]
        sets[key] = {
            "exercise_id": ex_id,
            "exercise": exercise.strip(),
            "set_number": set_number,
            "reps": reps,
            "weight": weight,
        }

    form: dict = {"date": sess["date"], "notes": sess["notes"] or ""}
    for (eid, num), s in sets.items():
        form[f"reps-{eid}-{num}"] = str(s["reps"])
        if s["weight"] is not None:
            form[f"weight-{eid}-{num}"] = str(s["weight"])

    if reps <= 0 and weight is None:
        # The app only deletes a set row when its reps-EX-N key is submitted
        # with an empty value; omitting it leaves the row untouched.
        form[f"reps-{ex_id}-{set_number}"] = ""

    r = await _authed("POST", f"/sessions/edit/{session_id}", data=form)
    if r.status_code == 404:
        raise ValueError("session not found")
    if r.status_code != 303:
        raise ValueError(_fail(r, "could not edit session"))
    return f"Updated set {set_number} of {exercise!r} in session {session_id}"


@mcp.tool()
async def delete_session(session_id: int) -> str:
    """Delete a workout session and all its sets.

    Args:
        session_id: id of the session to delete.
    """
    r = await _authed("POST", f"/sessions/{session_id}/delete")
    if r.status_code == 404:
        raise ValueError("session not found")
    if r.status_code != 303:
        raise ValueError(_fail(r, "could not delete session"))
    return f"Deleted session {session_id}"


# ---------- Cardio ----------

def _fmt_duration(mins) -> str:
    if mins is None:
        return ""
    mins = float(mins)
    total = int(round(mins * 60))
    m, s = divmod(total, 60)
    return f"{m} min" if s == 0 else f"{m}:{s:02d}"


def _cardio_summary(d: dict) -> str:
    dist = f"{d['distance_km']} km" if d.get("distance_km") is not None else ""
    dur = _fmt_duration(d.get("duration_min"))
    bits = [b for b in (dist, dur) if b]
    p = d.get("pace")
    unit = d.get("pace_unit", "km")
    pace = f" ({p} min/{unit})" if p else ""
    return f"{d['activity_type'].capitalize()} {d.get('date', '')} {' '.join(bits)}{pace}".strip()


@mcp.tool()
async def log_cardio(
    activity_type: str,
    distance_km: Optional[float] = None,
    duration_min: Optional[Union[float, str]] = None,
    date: Optional[str] = None,
    notes: Optional[str] = None,
) -> str:
    """Log a cardio / endurance activity (running, swimming, cycling, ...).

    At least one of distance_km or duration_min is required. The activity is
    attached to the session for the given date (created if needed).

    Args:
        activity_type: e.g. "running", "swimming", "cycling", "walking".
        distance_km: distance in kilometres (optional).
        duration_min: duration in minutes, e.g. 45 or "44:51" (optional).
        date: activity date as YYYY-MM-DD (defaults to today).
        notes: optional notes.
    """
    payload: dict = {"activity_type": activity_type.strip()}
    if distance_km is not None:
        payload["distance_km"] = distance_km
    if duration_min is not None:
        payload["duration_min"] = duration_min
    if date:
        payload["date"] = date
    if notes:
        payload["notes"] = notes
    r = await _authed(
        "POST", "/api/cardio",
        json=payload, headers={"Accept": "application/json"},
    )
    if r.status_code != 201:
        raise ValueError(_fail(r, "could not log cardio activity"))
    d = r.json()
    return f"Logged cardio activity {d['id']}: {_cardio_summary(d)}"


@mcp.tool()
async def list_cardio(limit: int = 10) -> list:
    """List recent cardio activities, newest first.

    Args:
        limit: max number of activities to return (default 10).
    """
    r = await _authed("GET", "/api/cardio", params={"limit": limit})
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def get_cardio(cardio_id: int) -> dict:
    """Get one cardio activity by id.

    Args:
        cardio_id: id of the activity (see list_cardio).
    """
    r = await _authed("GET", f"/api/cardio/{cardio_id}")
    if r.status_code == 404:
        raise ValueError("cardio activity not found")
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def delete_cardio(cardio_id: int) -> str:
    """Delete a cardio activity.

    Args:
        cardio_id: id of the activity to delete.
    """
    r = await _authed("DELETE", f"/api/cardio/{cardio_id}")
    if r.status_code == 404:
        raise ValueError("cardio activity not found")
    if r.status_code != 200:
        raise ValueError(_fail(r, "could not delete cardio activity"))
    return f"Deleted cardio activity {cardio_id}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
