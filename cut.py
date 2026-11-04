"""End-to-end cut: session -> storyboard -> (narration) -> MP4 + SRT + poster + JSON sidecar."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import render, storyboard, tts

NARRATION_PAD = 0.45  # seconds of air after each spoken line


def _narration_line(beat: dict) -> str:
    k, cap = beat["kind"], beat["caption"]
    if k == "title":
        return cap
    if k == "ask":
        first = re.split(r"(?<=[.!?])\s+", cap, maxsplit=1)[0]
        return "You asked: " + first
    if k == "outro":
        return "Cut by Showreel, for Hermes Agent."
    if k == "result":
        return re.split(r"(?<=[.!?])\s+", cap, maxsplit=1)[0]
    return re.sub(r"\s*\(\+\d+ / -\d+\)", "", cap)


def _retime(board: dict) -> None:
    t = 0.0
    for b in board["beats"]:
        b["start"] = round(t, 3)
        t += b["duration"]
    board["duration"] = round(t, 3)


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "session").strip("-").lower()
    return (s[:40] or "session").strip("-")


def cut(session: dict, out_dir: Path, *, length_s: float = 45.0, aspect: str = "16:9", theme: str = "midnight",
        narrator: Optional[tts.Narrator] = None, title: Optional[str] = None, ffmpeg: str = "",
        progress: Optional[Callable[[str], None]] = None) -> dict:
    t_start = time.monotonic()
    board = storyboard.build(session, length_s=length_s, title=title)
    if not board["beats"]:
        raise render.RenderError("nothing to cut: the session has no recorded events")
    rnd = render.Renderer(aspect=aspect, theme=theme, ffmpeg=ffmpeg)
    if not rnd.ffmpeg:
        raise render.RenderError("ffmpeg not found on PATH. Install it (apt install ffmpeg / brew install ffmpeg / "
                                 "winget install ffmpeg) or set plugins.entries.showreel.settings.ffmpeg_path.")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = out_dir / f"showreel-{_slug(board['title'])}-{aspect.replace(':', 'x')}-{stamp}"
    audio_path: Optional[Path] = None
    narration = "captions only"
    notes: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="showreel-", dir=str(out_dir)))
    try:
        if narrator is not None and narrator.engine != "off":
            if progress:
                progress("narrating")
            clips: list[tuple[int, Path]] = []
            for i, b in enumerate(board["beats"]):
                clip = narrator.say(_narration_line(b), work / f"beat{i:02d}")
                if clip is None:
                    if narrator.engine == "off":
                        break
                    continue
                need = render.audio_seconds(clip) + NARRATION_PAD + 0.25
                if need > b["duration"]:
                    b["duration"] = round(need, 3)
                clips.append((i, clip))
            notes += narrator.notes
            _retime(board)
            # Keep the hard cap: drop the weakest middle beats (and their clips) until it fits.
            while board["duration"] > storyboard.HARD_CAP_S:
                mids = [(b["score"], i) for i, b in enumerate(board["beats"])
                        if b["kind"] not in ("title", "ask", "result", "stats", "outro")]
                if not mids:
                    break
                _s, drop = min(mids)
                board["beats"].pop(drop)
                clips = [(i if i < drop else i - 1, c) for i, c in clips if i != drop]
                _retime(board)
            if board["duration"] > storyboard.HARD_CAP_S:  # last resort: shorten every beat evenly
                k = storyboard.HARD_CAP_S / board["duration"]
                for b in board["beats"]:
                    b["duration"] *= k
                _retime(board)
            if clips:
                placed = [(board["beats"][i]["start"] + 0.25, c) for i, c in clips]
                audio_path = render.mix_narration(placed, board["duration"], work / "narration.wav", rnd.ffmpeg)
                narration = narrator.describe()
        if progress:
            progress("rendering")
        info = rnd.render(board, base.with_suffix(".mp4"), audio_path=audio_path)
        srt = render.write_srt(board, base.with_suffix(".srt"))
        poster = rnd.poster(board, base.with_suffix(".png"), beat_index=0)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    result = {
        "success": True,
        "file_path": str(base.with_suffix(".mp4")),
        "srt_path": str(srt),
        "poster_path": str(poster),
        "duration_s": round(board["duration"], 2),
        "aspect": aspect,
        "beats": [{"kind": b["kind"], "caption": b["caption"], "start": b["start"], "duration": round(b["duration"], 2)}
                  for b in board["beats"]],
        "narration": narration,
        "redactions": board["stats"].get("redactions", 0),
        "render_seconds": round(time.monotonic() - t_start, 1),
        "frames": info["frames"],
        "notes": notes,
    }
    if narrator is not None and narrator.chars_sent:
        result["elevenlabs_characters"] = narrator.chars_sent
    base.with_suffix(".json").write_text(json.dumps({"storyboard": board, "result": result}, indent=2, default=str),
                                         encoding="utf-8")
    result["media_tag"] = "MEDIA:" + result["file_path"]
    return result
