"""Showreel backend for the Desktop half, mounted at /api/plugins/showreel/.

Routes read the plugin's own recordings and renders only. Rendering runs on a worker thread so the
gateway's event loop stays free. Nothing here makes a network call; narration follows the user's
Showreel settings (default off), and ElevenLabs runs only when the request names it explicitly.
"""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

router = APIRouter()

_PKG_DIR = Path(__file__).resolve().parent.parent
MAX_INLINE_BYTES = 48 * 1024 * 1024  # a 90 s 1080p cut is ~5-15 MB


def _app():
    """The Showreel instance the agent half registered in this process (no second copy is loaded)."""
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if name.startswith("hermes_plugins.") and f and Path(f).resolve() == _PKG_DIR / "__init__.py":
            app = getattr(mod, "_APP", None)
            if app is not None:
                return app
    raise HTTPException(503, "Showreel's agent half is not loaded here. Enable it with: hermes plugins enable showreel")


@router.get("/status")
async def status():
    return _app().status_dict()


@router.get("/sessions")
async def sessions(limit: int = 30):
    rows = _app().store.list_sessions(limit=max(1, min(int(limit), 200)))
    for r in rows:
        r["first_prompt"] = (r.get("first_prompt") or "")[:160]
    return {"sessions": rows}


@router.get("/plan")
async def plan(session_id: str = "", length: float = 0, narration: str = ""):
    res = await asyncio.to_thread(_app().plan_session, session_id, length=length or None,
                                  narration=narration or None)
    return res


@router.post("/render")
async def render(body: dict):
    body = body or {}
    narration = body.get("narration") or None
    if narration not in (None, "off", "auto", "local", "elevenlabs", "hermes"):
        raise HTTPException(400, "unknown narration engine")
    edits = body.get("edits") if isinstance(body.get("edits"), dict) else None
    res = await asyncio.to_thread(
        _app().render_session, str(body.get("session_id") or ""), length=body.get("length") or None,
        aspect=body.get("aspect") or None, title=body.get("title") or None, narration=narration, edits=edits)
    return _bare(res)


def _bare(res: dict) -> dict:
    """The Desktop only needs file names; absolute paths stay on the server."""
    out = dict(res)
    out.pop("media_tag", None)  # the chat MEDIA: tag carries the absolute path; the page does not need it
    for key in ("file_path", "srt_path", "poster_path"):
        if out.get(key):
            out[key.replace("_path", "_name") if key != "file_path" else "name"] = Path(out.pop(key)).name
        else:
            out.pop(key, None)
    return out


@router.get("/renders")
async def renders(limit: int = 30):
    return {"renders": _app().renders(limit=max(1, min(int(limit), 100)))}


@router.get("/media")
async def media(name: str):
    """One render (mp4 or poster png) as a data URL for the inline player."""
    f: Optional[Path] = _app().render_file(name)
    if f is None or f.suffix not in (".mp4", ".png"):
        raise HTTPException(404, "no such render")
    size = f.stat().st_size
    if size > MAX_INLINE_BYTES:
        raise HTTPException(413, "render too large to preview inline; open the file instead")
    data = await asyncio.to_thread(f.read_bytes)
    mime = "video/mp4" if f.suffix == ".mp4" else "image/png"
    return {"name": f.name, "url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii")}
