"""Session recorder: Hermes hook callbacks -> a scrubbed timeline in the plugin's own SQLite DB.

Everything is stored under ``<HERMES_HOME>/plugin-data/showreel/data.db`` through
``plugins.plugin_storage.plugin_db``. Every string is scrubbed (``redact.scrub``) BEFORE it is
written, large tool output is truncated, and hooks never raise into the agent loop.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import redact

logger = logging.getLogger(__name__)

PLUGIN_NAME = "showreel"
MAX_TEXT = 6000          # chars kept per text field
MAX_ARGS = 8000          # chars of JSON kept per tool-call args
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_PATH_RE = re.compile(r"(?:MEDIA:)?((?:/|~/)[^\s\"'<>|]{1,300}?\.(?:png|jpe?g|webp|gif))(?![A-Za-z0-9])", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started REAL NOT NULL,
    ended REAL,
    model TEXT,
    platform TEXT,
    completed INTEGER,
    first_prompt TEXT,
    redactions INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    tool TEXT,
    data TEXT NOT NULL,
    redactions INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id);
"""


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [{len(text) - limit} more chars]"


def _default_connect() -> sqlite3.Connection:
    from plugins.plugin_storage import plugin_db  # documented public helper
    return plugin_db(PLUGIN_NAME)


class Store:
    """Thread-safe wrapper around the plugin database."""

    def __init__(self, path: Optional[Path] = None, *, emails: bool = True):
        self._lock = threading.RLock()
        self._path = path
        self._conn: Optional[sqlite3.Connection] = None
        self.emails = emails

    # -- connection -------------------------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            else:
                self._conn = _default_connect()
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # -- writes -----------------------------------------------------------------------------
    def ensure_session(self, session_id: str, *, model: str = "", platform: str = "") -> None:
        with self._lock:
            db = self._db()
            db.execute(
                "INSERT OR IGNORE INTO sessions(session_id, started, model, platform) VALUES (?,?,?,?)",
                (session_id, time.time(), model or None, platform or None))
            if model or platform:
                db.execute(
                    "UPDATE sessions SET model=COALESCE(model, ?), platform=COALESCE(platform, ?) WHERE session_id=?",
                    (model or None, platform or None, session_id))
            db.commit()

    def add_event(self, session_id: str, kind: str, data: dict, *, tool: Optional[str] = None,
                  ts: Optional[float] = None) -> int:
        clean, n = redact.scrub_obj(data, emails=self.emails)
        payload = json.dumps(clean, ensure_ascii=False, default=str)
        with self._lock:
            db = self._db()
            self.ensure_session(session_id)
            cur = db.execute(
                "INSERT INTO events(session_id, ts, kind, tool, data, redactions) VALUES (?,?,?,?,?,?)",
                (session_id, ts or time.time(), kind, tool, payload, n))
            if n:
                db.execute("UPDATE sessions SET redactions = redactions + ? WHERE session_id=?", (n, session_id))
            if kind == "ask":
                db.execute("UPDATE sessions SET first_prompt=COALESCE(first_prompt, ?) WHERE session_id=?",
                           (clean.get("text", "")[:300], session_id))
            db.commit()
            return int(cur.lastrowid)

    def end_session(self, session_id: str, *, completed: Optional[bool] = None) -> None:
        with self._lock:
            db = self._db()
            self.ensure_session(session_id)
            db.execute("UPDATE sessions SET ended=?, completed=COALESCE(?, completed) WHERE session_id=?",
                       (time.time(), None if completed is None else int(bool(completed)), session_id))
            db.commit()

    def prune(self, keep: int) -> int:
        """Keep the newest *keep* sessions; return how many were deleted."""
        with self._lock:
            db = self._db()
            rows = db.execute("SELECT session_id FROM sessions ORDER BY started DESC").fetchall()
            old = [r[0] for r in rows[max(keep, 0):]]
            for sid in old:
                db.execute("DELETE FROM events WHERE session_id=?", (sid,))
                db.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
            db.commit()
            return len(old)

    def purge(self, session_id: Optional[str] = None) -> int:
        with self._lock:
            db = self._db()
            if session_id:
                n = db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,)).rowcount
                db.execute("DELETE FROM events WHERE session_id=?", (session_id,))
            else:
                n = db.execute("DELETE FROM sessions").rowcount
                db.execute("DELETE FROM events")
            db.commit()
            return int(n or 0)

    # -- reads ------------------------------------------------------------------------------
    def list_sessions(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db().execute(
                "SELECT s.session_id, s.started, s.ended, s.model, s.platform, s.completed, s.first_prompt,"
                " s.redactions, (SELECT COUNT(*) FROM events e WHERE e.session_id=s.session_id AND e.kind='tool')"
                " FROM sessions s ORDER BY s.started DESC LIMIT ?", (limit,)).fetchall()
        keys = ("session_id", "started", "ended", "model", "platform", "completed", "first_prompt",
                "redactions", "tool_calls")
        return [dict(zip(keys, r)) for r in rows]

    def latest_session_id(self, *, with_tools: bool = True) -> Optional[str]:
        for s in self.list_sessions(limit=50):
            if not with_tools or s["tool_calls"]:
                return s["session_id"]
        return None

    def get_session(self, session_id: str) -> Optional[dict]:
        with self._lock:
            db = self._db()
            row = db.execute(
                "SELECT session_id, started, ended, model, platform, completed, first_prompt, redactions"
                " FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                return None
            events = db.execute(
                "SELECT id, ts, kind, tool, data, redactions FROM events WHERE session_id=? ORDER BY id",
                (session_id,)).fetchall()
        keys = ("session_id", "started", "ended", "model", "platform", "completed", "first_prompt", "redactions")
        out = dict(zip(keys, row))
        out["events"] = [
            {"id": e[0], "ts": e[1], "kind": e[2], "tool": e[3], "data": json.loads(e[4]), "redactions": e[5]}
            for e in events]
        return out


# ── hook callbacks ──────────────────────────────────────────────────────────────────────────

def _parse_result(result: Any) -> tuple[Any, bool, Optional[int]]:
    """Return (parsed_or_text, ok, exit_code) for a tool result JSON string."""
    parsed: Any = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            parsed = result
    ok = True
    exit_code: Optional[int] = None
    if isinstance(parsed, dict):
        for k in ("exit_code", "returncode", "exitCode"):
            if isinstance(parsed.get(k), int):
                exit_code = parsed[k]
                ok = exit_code == 0
        if parsed.get("error") or parsed.get("success") is False:
            ok = False
    return parsed, ok, exit_code


def _result_text(parsed: Any) -> str:
    if isinstance(parsed, dict):
        for k in ("output", "stdout", "content", "result", "text", "message", "error"):
            v = parsed.get(k)
            if isinstance(v, str) and v.strip():
                return v
        return json.dumps(parsed, ensure_ascii=False, default=str)
    return str(parsed)


def find_images(*texts: str) -> list[str]:
    seen: list[str] = []
    for t in texts:
        for m in _PATH_RE.finditer(t or ""):
            p = Path(m.group(1)).expanduser()
            if p.suffix.lower() in IMAGE_EXT and p.is_file() and str(p) not in seen:
                seen.append(str(p))
    return seen[:4]


class Recorder:
    """Hook handlers. ``enabled`` is a zero-arg callable read on every event (live toggle)."""

    def __init__(self, store: Store, enabled: Callable[[], bool] = lambda: True):
        self.store = store
        self.enabled = enabled

    def _sid(self, kw: dict) -> str:
        return str(kw.get("session_id") or kw.get("task_id") or "")

    def _safe(self, fn: Callable[[], None]) -> None:
        try:
            if self.enabled():
                fn()
        except Exception as exc:  # never break the agent loop
            logger.debug("showreel capture skipped an event: %s", type(exc).__name__)

    # on_session_start(session_id, model, platform)
    def on_session_start(self, **kw: Any) -> None:
        sid = self._sid(kw)
        if sid:
            self._safe(lambda: self.store.ensure_session(sid, model=str(kw.get("model") or ""),
                                                         platform=str(kw.get("platform") or "")))

    # pre_llm_call(session_id, user_message, is_first_turn, model, platform, ...)
    def pre_llm_call(self, **kw: Any) -> None:
        sid = self._sid(kw)
        msg = kw.get("user_message")
        if not sid or not isinstance(msg, str) or not msg.strip():
            return None
        def _do() -> None:
            self.store.ensure_session(sid, model=str(kw.get("model") or ""), platform=str(kw.get("platform") or ""))
            self.store.add_event(sid, "ask", {"text": _clip(msg.strip(), MAX_TEXT),
                                              "first": bool(kw.get("is_first_turn"))})
        self._safe(_do)
        return None  # observer only: never inject context

    # post_tool_call(tool_name, args, result, task_id, duration_ms, session_id, status, ...)
    def post_tool_call(self, **kw: Any) -> None:
        sid = self._sid(kw)
        tool = str(kw.get("tool_name") or "")
        if not sid or not tool or tool.startswith("showreel"):
            return
        def _do() -> None:
            args = kw.get("args") if isinstance(kw.get("args"), dict) else {}
            parsed, ok, exit_code = _parse_result(kw.get("result"))
            status = kw.get("status")
            if isinstance(status, str) and status not in ("", "ok", "success", "completed"):
                ok = False
            text = _result_text(parsed)
            args_json = json.dumps(args, ensure_ascii=False, default=str)
            if len(args_json) > MAX_ARGS:
                args = {"_truncated": _clip(args_json, MAX_ARGS)}
            data = {
                "args": args,
                "output": _clip(text, MAX_TEXT),
                "ok": ok,
                "exit_code": exit_code,
                "duration_ms": int(kw.get("duration_ms") or 0),
                "images": find_images(text, args_json),
            }
            self.store.add_event(sid, "tool", data, tool=tool)
        self._safe(_do)

    # post_llm_call(session_id, user_message, assistant_response, ...)
    def post_llm_call(self, **kw: Any) -> None:
        sid = self._sid(kw)
        resp = kw.get("assistant_response")
        if not sid or not isinstance(resp, str) or not resp.strip():
            return
        self._safe(lambda: self.store.add_event(sid, "reply", {"text": _clip(resp.strip(), MAX_TEXT)}))

    # post_api_request(... usage, api_duration, response_model ...)
    def post_api_request(self, **kw: Any) -> None:
        sid = self._sid(kw)
        if not sid:
            return
        usage = kw.get("usage") if isinstance(kw.get("usage"), dict) else {}
        def _num(*keys: str) -> int:
            for k in keys:
                v = usage.get(k)
                if isinstance(v, (int, float)):
                    return int(v)
            return 0
        data = {
            "input_tokens": _num("input_tokens", "prompt_tokens"),
            "output_tokens": _num("output_tokens", "completion_tokens"),
            "duration_s": float(kw.get("api_duration") or 0.0),
            "model": str(kw.get("response_model") or kw.get("model") or ""),
        }
        self._safe(lambda: self.store.add_event(sid, "api", data))

    # on_session_end(session_id, completed, interrupted, ...)
    def on_session_end(self, **kw: Any) -> None:
        sid = self._sid(kw)
        if sid:
            self._safe(lambda: self.store.end_session(sid, completed=kw.get("completed")))
