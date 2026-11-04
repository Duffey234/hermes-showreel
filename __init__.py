"""Showreel for Hermes Agent: turn any session into a demo video under 90 seconds.

Hooks record what the session actually did (prompt, tool calls, diffs, terminal output, images,
token usage) into the plugin's own SQLite DB, scrubbed of secrets before anything is written.
``/showreel cut``, the ``showreel_render`` tool, or ``hermes showreel render`` then picks the key
beats, fits them to a hard time budget, and renders an MP4 (plus .srt captions and a poster)
locally with Pillow and the system ffmpeg. Optional narration: a free local voice, the user's own
ElevenLabs key, or Hermes's configured text_to_speech. Nothing is uploaded.
"""

from __future__ import annotations

import json
import logging
import shlex
from pathlib import Path
from typing import Any, Optional

from . import capture, cut, render, storyboard, tts

logger = logging.getLogger(__name__)

TOOL_NAME = "showreel_render"
ASPECTS = tuple(render.SIZES)

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Render a short demo video (MP4 with burned-in captions, plus an .srt and a poster PNG) of a "
        "Hermes session that Showreel recorded: the ask, key terminal commands, file edits, screenshots, "
        "the result and stats. Secrets are redacted before drawing; rendering is local. Defaults to the "
        "current session. Returns file_path and a MEDIA: tag. Use when the user asks for a demo video, "
        "recap video, reel, or clip of what was done."),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session to cut. Omit for the current one."},
            "length_seconds": {"type": "integer", "minimum": 10, "maximum": 89,
                               "description": "Target length in seconds (hard cap 89)."},
            "aspect": {"type": "string", "enum": list(ASPECTS), "description": "16:9, 9:16 or 1:1."},
            "title": {"type": "string", "description": "Optional title card text."},
            "narration": {"type": "string", "enum": list(tts.ENGINES),
                          "description": "Override the narration setting for this render."},
        },
        "required": [],
    },
}

USAGE = (
    "Usage: /showreel cut [seconds] [16:9|9:16|1:1] [title...]   render this session\n"
    "       /showreel list                 recorded sessions\n"
    "       /showreel status               capture, narration and ffmpeg status\n"
    "       /showreel demo [seconds] [aspect]  render the built-in sample session\n"
    "       /showreel plan [seconds]       preview beats and narration text (no audio, no video)\n"
    "       /showreel purge [session_id]   delete recordings (all, or one)")


class Showreel:
    def __init__(self, ctx: Any, store: Optional[capture.Store] = None):
        self.ctx = ctx
        self.store = store or capture.Store(emails=bool(self.cfg("redact_emails", True)))
        self.recorder = capture.Recorder(self.store, enabled=lambda: bool(self.cfg("capture", True)))
        self._hooked_sessions: set[str] = set()

    # ── settings ───────────────────────────────────────────────────────────────────────────
    def cfg(self, key: str, default: Any) -> Any:
        try:
            v = self.ctx.get_config(key, default)
        except Exception:
            return default
        return default if v is None else v

    def out_dir(self) -> Path:
        custom = str(self.cfg("output_dir", "") or "").strip()
        if custom:
            return Path(custom).expanduser()
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir(capture.PLUGIN_NAME) / "renders"

    def unattended(self) -> bool:
        try:
            return self.ctx.current_cron_execution() is not None
        except Exception:
            return False

    def narrator(self, override: Optional[str] = None) -> tts.Narrator:
        engine = str(override or self.cfg("narration", "off") or "off")
        if engine not in tts.ENGINES:
            engine = "off"
        n = tts.Narrator(
            engine, voice=str(self.cfg("elevenlabs_voice_id", "") or ""),
            model=str(self.cfg("elevenlabs_model", "") or ""),
            local_voice=str(self.cfg("local_voice", "en-us") or "en-us"),
            piper_model=str(self.cfg("piper_model", "") or ""),
            dispatch_tool=getattr(self.ctx, "dispatch_tool", None))
        if n.engine == "elevenlabs" and self.unattended() and not bool(self.cfg("paid_narration_unattended", False)):
            # Paid calls never happen in cron/unattended runs unless the user opted in.
            n.engine = "local" if tts.local_backend(n.piper_model) else "off"
            n.notes.append("unattended run: ElevenLabs skipped (set paid_narration_unattended to allow)")
        return n

    # ── operations ─────────────────────────────────────────────────────────────────────────
    def render_session(self, session_id: str = "", *, length: Optional[float] = None, aspect: Optional[str] = None,
                       title: Optional[str] = None, narration: Optional[str] = None,
                       edits: Optional[dict] = None) -> dict:
        sid = session_id or self.store.latest_session_id()
        if not sid:
            return {"success": False, "error": "No recorded session yet. Showreel records sessions while it is "
                                               "enabled; run a task first, or try /showreel demo."}
        session = self.store.get_session(sid)
        if not session or not session["events"]:
            return {"success": False, "error": f"Session {sid} has no recorded events."}
        aspect = aspect or str(self.cfg("default_aspect", "16:9"))
        if aspect not in ASPECTS:
            return {"success": False, "error": f"aspect must be one of {', '.join(ASPECTS)}"}
        length = float(length or self.cfg("default_length", 45))
        try:
            res = cut.cut(session, self.out_dir(), length_s=length, aspect=aspect,
                          theme=str(self.cfg("theme", "midnight")), narrator=self.narrator(narration),
                          title=title, ffmpeg=str(self.cfg("ffmpeg_path", "") or ""),
                          resolution=str(self.cfg("resolution", "1080p") or "1080p"), edits=edits)
        except (render.RenderError, tts.TTSError) as exc:
            return {"success": False, "error": str(exc)}
        res["session_id"] = sid
        try:
            self.store.prune(int(self.cfg("keep_sessions", 30)))
        except Exception:
            pass
        return res

    def plan_session(self, session_id: str = "", *, length: Optional[float] = None, title: Optional[str] = None,
                     narration: Optional[str] = None, demo: bool = False, edits: Optional[dict] = None) -> dict:
        if demo:
            from .sample import record_sample
            session_id = record_sample(capture.Recorder(self.store))
        sid = session_id or self.store.latest_session_id()
        session = self.store.get_session(sid) if sid else None
        if not session or not session["events"]:
            return {"success": False, "error": "No recorded session to plan."}
        try:
            res = cut.preview(session, length_s=float(length or self.cfg("default_length", 45)),
                              narrator=self.narrator(narration), title=title, edits=edits)
        except (render.RenderError, tts.TTSError) as exc:
            return {"success": False, "error": str(exc)}
        res["session_id"] = sid
        return res

    def render_demo(self, *, length: Optional[float] = None, aspect: Optional[str] = None,
                    narration: Optional[str] = None) -> dict:
        from .sample import record_sample
        sid = record_sample(capture.Recorder(self.store))  # demo always records, even with capture off
        return self.render_session(sid, length=length, aspect=aspect, narration=narration)

    RECORDING_WINDOW_S = 120.0

    def status_dict(self) -> dict:
        """Machine-readable status for the Desktop half (REC chip, page header)."""
        import time as _t
        last = self.store.last_event()
        capture_on = bool(self.cfg("capture", True))
        recording = bool(capture_on and last and _t.time() - last[1] < self.RECORDING_WINDOW_S)
        n = self.narrator()
        return {
            "capture": capture_on,
            "recording": recording,
            "recording_session": last[0] if recording and last else None,
            "sessions": len(self.store.list_sessions(limit=1000)),
            "ffmpeg": bool(render.find_ffmpeg(str(self.cfg("ffmpeg_path", "") or ""))),
            "narration": str(self.cfg("narration", "off")),
            "narration_resolved": n.describe(),
            "offline_voice": bool(tts.local_backend(str(self.cfg("piper_model", "") or ""))),
            "default_length": float(self.cfg("default_length", 45)),
            "default_aspect": str(self.cfg("default_aspect", "16:9")),
        }

    def renders(self, limit: int = 30) -> list[dict]:
        """Rendered videos, newest first, with their JSON sidecar summary."""
        out_dir = self.out_dir()
        if not out_dir.is_dir():
            return []
        rows = []
        for mp4 in sorted(out_dir.glob("showreel-*.mp4"), key=lambda q: q.stat().st_mtime, reverse=True)[:limit]:
            meta: dict = {}
            side = mp4.with_suffix(".json")
            if side.is_file():
                try:
                    meta = json.loads(side.read_text(encoding="utf-8")).get("result") or {}
                except (OSError, ValueError):
                    meta = {}
            rows.append({"name": mp4.name, "path": str(mp4), "bytes": mp4.stat().st_size,
                         "mtime": mp4.stat().st_mtime, "duration_s": meta.get("duration_s"),
                         "aspect": meta.get("aspect"), "narration": meta.get("narration"),
                         "session_id": meta.get("session_id"),
                         "poster": mp4.with_suffix(".png").name if mp4.with_suffix(".png").is_file() else None})
        return rows

    def render_file(self, name: str) -> Optional[Path]:
        """A file in the renders folder by bare name (no paths, no traversal)."""
        if not name or "/" in name or "\\" in name or name.startswith(".") or not name.startswith("showreel-"):
            return None
        if not name.endswith((".mp4", ".png", ".srt", ".json")):
            return None
        f = self.out_dir() / name
        try:
            f.resolve().relative_to(self.out_dir().resolve())
        except (OSError, ValueError):
            return None
        return f if f.is_file() else None

    def status(self) -> str:
        ff = render.find_ffmpeg(str(self.cfg("ffmpeg_path", "") or ""))
        n = self.narrator()
        sessions = self.store.list_sessions(limit=1000)
        local = tts.local_backend(str(self.cfg("piper_model", "") or ""))
        lines = [
            f"Showreel capture: {'ON' if self.cfg('capture', True) else 'off'} · {len(sessions)} recorded session(s)",
            f"ffmpeg: {ff or 'NOT FOUND (install ffmpeg to render)'}",
            f"narration: {self.cfg('narration', 'off')} -> {n.describe()}",
            f"offline voice: {local[0] + ' at ' + local[1] if local else 'none (install espeak-ng or piper)'}",
            f"renders go to: {self.out_dir()}",
        ]
        return "\n".join(lines)

    def list_text(self, limit: int = 10) -> str:
        rows = self.store.list_sessions(limit=limit)
        if not rows:
            return "No recorded sessions yet."
        import time as _t
        out = []
        for r in rows:
            when = _t.strftime("%b %d %H:%M", _t.localtime(r["started"] or 0))
            prompt = (r["first_prompt"] or "").replace("\n", " ")[:60]
            out.append(f"{r['session_id']}  {when}  {r['tool_calls']} tools  {prompt}")
        return "\n".join(out)

    # ── tool / slash / CLI entry points ─────────────────────────────────────────────────────
    def tool_handler(self, args: dict, **kw: Any) -> str:
        args = args or {}
        sid = str(args.get("session_id") or kw.get("session_id") or kw.get("task_id") or "")
        res = self.render_session(sid, length=args.get("length_seconds"), aspect=args.get("aspect"),
                                  title=args.get("title"), narration=args.get("narration"))
        return json.dumps(res, default=str)

    def slash(self, raw_args: str = "", **kw: Any) -> str:
        try:
            parts = shlex.split(raw_args or "")
        except ValueError:
            parts = (raw_args or "").split()
        sub = parts[0].lower() if parts else "cut"
        rest = parts[1:]
        if sub in ("help", "-h", "--help"):
            return USAGE
        if sub == "status":
            return self.status()
        if sub == "list":
            return self.list_text()
        if sub == "purge":
            n = self.store.purge(rest[0] if rest else None)
            return f"Deleted {n} recorded session(s). Rendered videos were kept."
        if sub == "plan":
            length = next((float(p.rstrip("s")) for p in rest if p.rstrip("s").isdigit()), None)
            return format_plan(self.plan_session(str(kw.get("session_id") or ""), length=length))
        if sub in ("cut", "demo"):
            length, aspect, title_words = None, None, []
            for p in rest:
                if p in ASPECTS:
                    aspect = p
                elif p.rstrip("s").isdigit() and length is None:
                    length = float(p.rstrip("s"))
                else:
                    title_words.append(p)
            if sub == "demo":
                res = self.render_demo(length=length, aspect=aspect)
            else:
                sid = str(kw.get("session_id") or "")
                res = self.render_session(sid, length=length, aspect=aspect, title=" ".join(title_words) or None)
            return format_result(res)
        return USAGE


def format_result(res: dict) -> str:
    if not res.get("success"):
        return f"Showreel: {res.get('error', 'failed')}"
    beats = " → ".join(b["kind"] for b in res["beats"])
    lines = [
        f"Showreel cut: {res['duration_s']}s {res['aspect']} · {len(res['beats'])} beats ({beats})",
        f"narration: {res['narration']} · {res['redactions']} secret(s) redacted · rendered in {res['render_seconds']}s",
        f"video: {res['file_path']}",
        f"captions: {res['srt_path']}",
    ]
    if res.get("elevenlabs_characters"):
        lines.append(f"ElevenLabs characters used: {res['elevenlabs_characters']} (billed to your ElevenLabs account)")
    for n in res.get("notes") or []:
        lines.append(f"note: {n}")
    lines.append(res["media_tag"])
    return "\n".join(lines)


def format_plan(res: dict) -> str:
    if not res.get("success"):
        return f"Showreel: {res.get('error', 'failed')}"
    p = res["plan"]
    lines = [f"Showreel plan: \"{res['title']}\" · {res['duration_s']}s of beats · target {p['target_s']}s · "
             f"narration: {res['narration']}"]
    for b in res["beats"]:
        say = f'  say: "{b["say"]}"' if b["say"] else "  (silent)"
        lines.append(f"{b['start']:6.1f}s {b['kind']:<8} {b['caption'][:70]}{say}")
    lines.append(f"~{p['estimated_speech_s']}s of speech, ~{p['estimated_duration_s']}s video, {p['characters']} characters"
                 + (f", {p['muted']} beat(s) silent to fit" if p["muted"] else "")
                 + (". Captions only: nothing would be spoken." if res["narration"].startswith("captions only")
                    else ". Nothing was synthesized."))
    return "\n".join(lines)


# ── CLI: hermes showreel ... ─────────────────────────────────────────────────────────────────
def _cli_setup(app_getter):
    def setup(subparser):
        subs = subparser.add_subparsers(dest="showreel_command")
        subs.add_parser("list", help="List recorded sessions")
        subs.add_parser("status", help="Capture, narration and ffmpeg status")
        r = subs.add_parser("render", help="Render a recorded session to MP4")
        r.add_argument("session_id", nargs="?", default="", help="Session id (default: latest with tool calls)")
        r.add_argument("--length", type=float, default=None, help="Target seconds (10-89)")
        r.add_argument("--aspect", choices=list(ASPECTS), default=None)
        r.add_argument("--title", default=None)
        r.add_argument("--narration", choices=list(tts.ENGINES), default=None)
        d = subs.add_parser("demo", help="Render the built-in sample session")
        d.add_argument("--length", type=float, default=None)
        d.add_argument("--aspect", choices=list(ASPECTS), default=None)
        d.add_argument("--narration", choices=list(tts.ENGINES), default=None)
        pl = subs.add_parser("plan", help="Preview beats and the narration text; no audio, no video, no network")
        pl.add_argument("session_id", nargs="?", default="")
        pl.add_argument("--length", type=float, default=None)
        pl.add_argument("--title", default=None)
        pl.add_argument("--narration", choices=list(tts.ENGINES), default=None,
                        help="Engine whose speaking rate to plan for (nothing is synthesized)")
        pl.add_argument("--demo", action="store_true", help="Plan the built-in sample session")
        pl.add_argument("--json", action="store_true")
        p = subs.add_parser("purge", help="Delete recordings (videos are kept)")
        p.add_argument("session_id", nargs="?", default=None)
        subparser.set_defaults(func=lambda a: _cli_run(app_getter(), a))
    return setup


def _cli_run(app: Showreel, args) -> int:
    sub = getattr(args, "showreel_command", None)
    if sub == "list":
        print(app.list_text(limit=50))
    elif sub == "status":
        print(app.status())
    elif sub == "render":
        print(format_result(app.render_session(args.session_id, length=args.length, aspect=args.aspect,
                                               title=args.title, narration=args.narration)))
    elif sub == "demo":
        print(format_result(app.render_demo(length=args.length, aspect=args.aspect, narration=args.narration)))
    elif sub == "plan":
        res = app.plan_session(args.session_id, length=args.length, title=args.title, narration=args.narration,
                               demo=args.demo)
        print(json.dumps(res, indent=2, default=str) if args.json else format_plan(res))
    elif sub == "purge":
        print(f"Deleted {app.store.purge(args.session_id)} recorded session(s).")
    else:
        print(USAGE.replace("/showreel", "hermes showreel"))
        return 1
    return 0


_APP: Optional[Showreel] = None


def register(ctx) -> None:
    global _APP
    app = Showreel(ctx)
    _APP = app
    rec = app.recorder
    ctx.register_hook("on_session_start", rec.on_session_start)
    ctx.register_hook("pre_llm_call", rec.pre_llm_call)
    ctx.register_hook("post_tool_call", rec.post_tool_call)
    ctx.register_hook("post_api_request", rec.post_api_request)
    ctx.register_hook("post_llm_call", rec.post_llm_call)
    ctx.register_hook("on_session_end", rec.on_session_end)
    ctx.register_tool(name=TOOL_NAME, toolset="showreel", schema=TOOL_SCHEMA, handler=app.tool_handler,
                      description=TOOL_SCHEMA["description"], emoji="🎬")
    ctx.register_command("showreel", app.slash, description="Cut this session into a demo video",
                         args_hint="[cut|list|status|demo|purge] [seconds] [aspect]")
    ctx.register_cli_command(name="showreel", help="Turn recorded Hermes sessions into demo videos",
                             setup_fn=_cli_setup(lambda: app), handler_fn=None,
                             description="List, render and purge Showreel recordings.")
    try:
        ctx.on_unload(app.store.close)
    except Exception:
        pass
