"""SQLite persistence. One connection, guarded by a lock (calls are short)."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS portals (
  id TEXT PRIMARY KEY, url TEXT NOT NULL, norm_url TEXT UNIQUE NOT NULL,
  state TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL,
  last_success_at TEXT, last_read_at TEXT, deleted INTEGER DEFAULT 0,
  recipe_version INTEGER DEFAULT 0, good_count INTEGER DEFAULT 0,
  good_profile TEXT, last_repair_at TEXT, hints TEXT
);
CREATE TABLE IF NOT EXISTS recipes (
  portal_id TEXT, version INTEGER, recipe TEXT, created_at TEXT, source TEXT,
  PRIMARY KEY (portal_id, version)
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, portal_id TEXT, kind TEXT, started_at TEXT, finished_at TEXT,
  result TEXT, reason TEXT, steps TEXT, llm_calls INTEGER DEFAULT 0,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0, tender_count INTEGER
);
CREATE INDEX IF NOT EXISTS runs_portal ON runs(portal_id, started_at);
CREATE TABLE IF NOT EXISTS tenders (
  portal_id TEXT, tender_id TEXT, data TEXT, hash TEXT, list_hash TEXT,
  last_changed_at TEXT, last_seen_at TEXT, listed INTEGER DEFAULT 1,
  PRIMARY KEY (portal_id, tender_id)
);
CREATE TABLE IF NOT EXISTS files (
  id TEXT PRIMARY KEY, sha256 TEXT UNIQUE, size INTEGER, mime TEXT, name TEXT
);
CREATE TABLE IF NOT EXISTS doc_cache (
  url TEXT PRIMARY KEY, fetched_at TEXT, entries TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(files)")}
            if "name" not in cols:  # migrate databases from v0.2
                self.conn.execute("ALTER TABLE files ADD COLUMN name TEXT")

    # ---------- generic helpers ----------
    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple = ()) -> None:
        with self.lock:
            self.conn.execute(sql, args)

    # ---------- portals ----------
    def create_portal(self, url: str, norm_url: str) -> tuple[str, bool]:
        """Return (id, created). Same normalized URL -> same id while the portal exists.
        A deleted portal is gone for good: adding it again starts a fresh attach."""
        with self.lock:
            row = self.conn.execute("SELECT id FROM portals WHERE norm_url=?",
                                    (norm_url,)).fetchone()
            if row:
                return row["id"], False
            pid = new_id("p_")
            self.conn.execute(
                "INSERT INTO portals(id,url,norm_url,state,reason,created_at) VALUES(?,?,?,?,?,?)",
                (pid, url, norm_url, "integrating", "Queued for attaching", now_iso()))
            return pid, True

    def get_portal(self, pid: str) -> dict | None:
        rows = self.q("SELECT * FROM portals WHERE id=? AND deleted=0", (pid,))
        return dict(rows[0]) if rows else None

    def list_portals(self) -> list[dict]:
        return [dict(r) for r in self.q("SELECT * FROM portals WHERE deleted=0 ORDER BY created_at")]

    def update_portal(self, pid: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        self.x(f"UPDATE portals SET {cols} WHERE id=?", (*vals, pid))

    def purge_portal(self, pid: str) -> None:
        """DELETE /portals/{id}: remove the portal with its recipes, runs and tenders.
        Running tasks notice via is_deleted() (row missing) before their next request."""
        with self.lock:
            for table, col in (("tenders", "portal_id"), ("recipes", "portal_id"),
                               ("runs", "portal_id"), ("portals", "id")):
                self.conn.execute(f"DELETE FROM {table} WHERE {col}=?", (pid,))

    def is_deleted(self, pid: str) -> bool:
        rows = self.q("SELECT deleted FROM portals WHERE id=?", (pid,))
        return not rows or bool(rows[0]["deleted"])

    def open_count(self, pid: str) -> int:
        return self.q("SELECT COUNT(*) c FROM tenders WHERE portal_id=? AND "
                      "json_extract(data, '$.status')='open'", (pid,))[0]["c"]

    def tender_count(self, pid: str) -> int:
        return self.q("SELECT COUNT(*) c FROM tenders WHERE portal_id=?", (pid,))[0]["c"]

    # ---------- recipes ----------
    def save_recipe(self, pid: str, recipe: dict, source: str) -> int:
        """New version, unless it is identical to the current one (then keep that)."""
        with self.lock:
            cur = self.get_recipe(pid)
            if cur is not None and json.dumps(cur, sort_keys=True) == json.dumps(recipe,
                                                                                 sort_keys=True):
                return self.conn.execute("SELECT recipe_version v FROM portals WHERE id=?",
                                         (pid,)).fetchone()["v"]
            row = self.conn.execute("SELECT MAX(version) v FROM recipes WHERE portal_id=?",
                                    (pid,)).fetchone()
            version = (row["v"] or 0) + 1
            self.conn.execute("INSERT INTO recipes VALUES(?,?,?,?,?)",
                              (pid, version, json.dumps(recipe), now_iso(), source))
            self.conn.execute("UPDATE portals SET recipe_version=? WHERE id=?", (version, pid))
            return version

    def get_recipe(self, pid: str, version: int | None = None) -> dict | None:
        if version is None:
            p = self.q("SELECT recipe_version FROM portals WHERE id=?", (pid,))
            if not p or not p[0]["recipe_version"]:
                return None
            version = p[0]["recipe_version"]
        rows = self.q("SELECT recipe FROM recipes WHERE portal_id=? AND version=?", (pid, version))
        return json.loads(rows[0]["recipe"]) if rows else None

    def list_recipes(self, pid: str) -> list[dict]:
        return [{"version": r["version"], "createdAt": r["created_at"], "source": r["source"],
                 "recipe": json.loads(r["recipe"])}
                for r in self.q("SELECT * FROM recipes WHERE portal_id=? ORDER BY version DESC",
                                (pid,))]

    def set_recipe_version(self, pid: str, version: int) -> None:
        self.x("UPDATE portals SET recipe_version=? WHERE id=?", (version, pid))

    # ---------- runs ----------
    def create_run(self, pid: str, kind: str) -> str:
        rid = new_id("r_")
        self.x("INSERT INTO runs(id,portal_id,kind,started_at,steps) VALUES(?,?,?,?,?)",
               (rid, pid, kind, now_iso(), "[]"))
        return rid

    def finish_run(self, rid: str, result: str, reason: str | None, steps: list[str],
                   usage: dict, tender_count: int | None) -> None:
        self.x("""UPDATE runs SET finished_at=?, result=?, reason=?, steps=?, llm_calls=?,
                  input_tokens=?, output_tokens=?, cost_usd=?, tender_count=? WHERE id=?""",
               (now_iso(), result, reason, json.dumps(steps), usage.get("calls", 0),
                usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                round(usage.get("cost_usd", 0.0), 5), tender_count, rid))

    def update_run_steps(self, rid: str, steps: list[str]) -> None:
        self.x("UPDATE runs SET steps=? WHERE id=?", (json.dumps(steps), rid))

    def list_runs(self, pid: str, limit: int = 200) -> list[dict]:
        rows = self.q("SELECT * FROM runs WHERE portal_id=? ORDER BY started_at DESC, rowid DESC "
                      "LIMIT ?", (pid, limit))
        return [dict(r) for r in rows]

    def recent_runs(self, pid: str, n: int = 16) -> list[dict]:
        """Last n runs, oldest first (for the dashboard's run-history strip)."""
        rows = self.q("SELECT id, kind, result, started_at, finished_at FROM runs WHERE portal_id=? "
                      "ORDER BY started_at DESC, rowid DESC LIMIT ?", (pid, n))
        return [{"id": r["id"], "kind": r["kind"], "result": r["result"],
                 "startedAt": r["started_at"], "finishedAt": r["finished_at"]}
                for r in reversed(rows)]

    def portal_cost(self, pid: str) -> dict:
        r = self.q("SELECT COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(llm_calls),0) n, "
                   "COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o "
                   "FROM runs WHERE portal_id=?", (pid,))[0]
        return {"costUsd": round(r["c"], 4), "llmCalls": r["n"], "inputTokens": r["i"],
                "outputTokens": r["o"]}

    def close_open_runs(self) -> None:
        """At startup: runs interrupted by a restart are failed."""
        self.x("UPDATE runs SET finished_at=?, result='failed', reason='Interrupted by restart' "
               "WHERE finished_at IS NULL", (now_iso(),))

    # ---------- tenders ----------
    def get_tenders(self, pid: str) -> dict[str, dict]:
        rows = self.q("SELECT * FROM tenders WHERE portal_id=?", (pid,))
        return {r["tender_id"]: dict(r) for r in rows}

    def upsert_tender(self, pid: str, tid: str, data: dict, h: str, list_hash: str,
                      changed_at: str, seen_at: str, listed: bool) -> None:
        self.x("""INSERT INTO tenders VALUES(?,?,?,?,?,?,?,?)
                  ON CONFLICT(portal_id, tender_id) DO UPDATE SET data=excluded.data,
                  hash=excluded.hash, list_hash=excluded.list_hash,
                  last_changed_at=excluded.last_changed_at, last_seen_at=excluded.last_seen_at,
                  listed=excluded.listed""",
               (pid, tid, json.dumps(data), h, list_hash, changed_at, seen_at, int(listed)))

    def tender_payloads(self, pid: str) -> list[dict]:
        rows = self.q("SELECT data FROM tenders WHERE portal_id=? ORDER BY last_changed_at DESC",
                      (pid,))
        return [json.loads(r["data"]) for r in rows]

    # ---------- files ----------
    def add_file(self, sha: str, size: int, mime: str | None, name: str | None = None) -> str:
        with self.lock:
            row = self.conn.execute("SELECT id FROM files WHERE sha256=?", (sha,)).fetchone()
            if row:
                return row["id"]
            fid = sha[:24]
            self.conn.execute("INSERT INTO files(id,sha256,size,mime,name) VALUES(?,?,?,?,?)",
                              (fid, sha, size, mime, name))
            return fid

    def get_file(self, fid: str) -> dict | None:
        rows = self.q("SELECT * FROM files WHERE id=?", (fid,))
        return dict(rows[0]) if rows else None

    def doc_cache_get(self, url: str) -> list[dict] | None:
        rows = self.q("SELECT entries FROM doc_cache WHERE url=?", (url,))
        return json.loads(rows[0]["entries"]) if rows else None

    def doc_cache_put(self, url: str, entries: list[dict]) -> None:
        self.x("INSERT OR REPLACE INTO doc_cache VALUES(?,?,?)",
               (url, now_iso(), json.dumps(entries)))
