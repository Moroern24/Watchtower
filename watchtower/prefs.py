"""Operator settings that can change while Watchtower runs (dashboard → Settings, `/settings`).

Stored in SQLite so they survive restarts. Environment variables only provide the defaults.
These settings tune the schedule; they never steer how a portal is read or repaired.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .config import settings
from .db import DB

# key: (type, min, max)
SPEC = {
    "autoCheck": (bool, None, None),
    "checkIntervalMin": (int, 1, 1440),        # regular read of every serving portal
    "repairCooldownMin": (int, 0, 10080),      # minimum gap between model repairs of a failed portal
}


def defaults() -> dict:
    return {"autoCheck": True, "checkIntervalMin": settings.read_interval_min,
            "repairCooldownMin": settings.repair_cooldown_min}


def get(db: DB) -> dict:
    out = defaults()
    for row in db.q("SELECT key, value FROM settings"):
        if row["key"] in SPEC:
            try:
                out[row["key"]] = json.loads(row["value"])
            except ValueError:
                pass
    return out


def update(db: DB, patch: dict) -> dict:
    """Validate and store a partial update. Raises ValueError with a readable message."""
    if not isinstance(patch, dict) or not patch:
        raise ValueError("Send a JSON object with at least one setting")
    clean = {}
    for key, value in patch.items():
        if key not in SPEC:
            raise ValueError(f"Unknown setting '{key}'")
        typ, lo, hi = SPEC[key]
        if typ is bool:
            if not isinstance(value, bool):
                raise ValueError(f"'{key}' must be true or false")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
                raise ValueError(f"'{key}' must be a whole number")
            value = int(value)
            if not lo <= value <= hi:
                raise ValueError(f"'{key}' must be between {lo} and {hi}")
        clean[key] = value
    for key, value in clean.items():
        db.x("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, json.dumps(value)))
    return get(db)


def next_check_at(portal: dict, p: dict, active: bool) -> str | None:
    """When the scheduler will read this portal next (None = not scheduled)."""
    if active or not p["autoCheck"]:
        return None
    hints = json.loads(portal.get("hints") or "{}")
    if portal["state"] == "serving":
        minutes = p["checkIntervalMin"]
    elif portal["state"] == "failed" and not hints.get("blocked"):
        minutes = p["checkIntervalMin"] * 2
    else:
        return None
    last = portal.get("last_read_at")
    if not last:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        return (datetime.fromisoformat(last) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    except ValueError:
        return None
