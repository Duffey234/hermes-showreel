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


def _sentences(text: str, min_words: int = 4) -> str:
    """Leading sentences of *text* until there are at least *min_words* words."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    out: list[str] = []
    for p in parts:
        out.append(p)
        if len(" ".join(out).split()) >= min_words:
            break
    return " ".join(out)


def _spoken(text: str) -> str:
    """Caption text cleaned for a voice: no markdown ticks, separators read as pauses."""
    t = text.replace("`", "")
    t = re.sub(r"\s*\(\+\d+ / -\d+\)", "", t)
    t = re.sub(r"\s+[\u00b7|]\s+", ", ", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    if t.endswith(("\u2026", "...")):  # a truncated caption: stop at the last clause that is whole
        cut_at = max(t.rfind(c) for c in ".;,!?")
        t = t[:cut_at].rstrip(" ,;") + "." if cut_at > 10 else t.rstrip("\u2026. ")
    return t[:1].upper() + t[1:] if t else t


def _narration_line(beat: dict) -> str:
    k, cap = beat["kind"], beat["caption"]
    if k == "title":
        return _spoken(cap)
    if k == "ask":
        return "You asked: " + _spoken(_sentences(cap, 3))
    if k == "outro":
        return "Cut by Showreel, for Hermes Agent."
    if k == "result":
        return _spoken(_sentences(cap))
    return _spoken(cap)


_PROTECTED = ("title", "ask", "result")
LEAD_IN = 0.25  # seconds of silence before each spoken line


MIN_BEAT_S = 2.0  # a beat is never squeezed below this when making room for speech


def _clause(text: str) -> str:
    """A shorter version of *text*: its first sentence of 3+ words, cut at the first clause break."""
    head = _sentences(text, 3)
    parts = re.split(r"[,;:]\s+|\s+[-\u2013\u2014]\s+", head)
    out = ""
    for p in parts:
        out = f"{out}, {p}" if out else p
        if len(out.split()) >= 3:
            break
    out = out.strip().rstrip(".,;:")
    return (out + ".") if out and len(out) < len(text.rstrip(".")) else text


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _short_line(beat: dict) -> str:
    line = _narration_line(beat)
    if beat["kind"] == "ask" and line.startswith("You asked: "):
        return "You asked: " + _clause(line[len("You asked: "):])
    return _clause(line)


def plan_narration(board: dict, target_s: float, estimate: Callable[[str], float]) -> dict:
    """Decide what will be spoken, and how long every beat lasts, BEFORE any audio exists.

    1. Every line gets an estimated spoken length; a line longer than its beat is cut to its
       first clause.
    2. Beats with speech grow to fit it. The time comes out of beats that have more time than they
       need (never below MIN_BEAT_S), so the video still matches the requested length.
    3. If that is not enough, the least important middle beats go silent (their caption stays on
       screen) until it fits. The title, the ask and the result are always spoken.
    Nothing is synthesized for a silent beat, so a paid voice is never billed for wasted audio.
    """
    beats = board["beats"]
    target = min(max(float(target_s or 0.0), storyboard.MIN_LEN_S), storyboard.HARD_CAP_S)
    lines: list[Optional[str]] = []
    for b in beats:
        line = _narration_line(b).strip() or None
        if line and estimate(line) + LEAD_IN + NARRATION_PAD > b["duration"]:
            line = _short_line(b).strip() or line
        lines.append(line)
    # The title usually repeats the ask; say it once ("You asked: ...").
    asks = [lines[i] for i, b in enumerate(beats) if b["kind"] == "ask" and lines[i]]
    for i, b in enumerate(beats):
        if b["kind"] == "title" and lines[i] and asks and _norm(lines[i]) in _norm(asks[0]):
            lines[i] = None

    def need(i: int) -> float:
        return estimate(lines[i]) + LEAD_IN + NARRATION_PAD if lines[i] else 0.0

    def fit() -> list[float]:
        want = [max(b["duration"], need(i)) for i, b in enumerate(beats)]
        floor = [max(MIN_BEAT_S, need(i)) if lines[i] else min(b["duration"], MIN_BEAT_S)
                 for i, b in enumerate(beats)]
        over = sum(want) - target
        room = sum(max(0.0, w - f) for w, f in zip(want, floor))
        if over > 0 and room > 0:
            k = min(1.0, over / room)
            want = [w - (w - f) * k if w > f else w for w, f in zip(want, floor)]
        return want

    durations = fit()
    muted = 0
    while sum(durations) > target + 0.05:
        cands = [(b.get("score", 0.0), i) for i, b in enumerate(beats) if lines[i] and b["kind"] not in _PROTECTED]
        if not cands:
            break
        lines[min(cands)[1]] = None
        muted += 1
        durations = fit()
    if sum(durations) > target + 0.05:  # only the story is left: say the short form of it
        for i, b in enumerate(beats):
            if lines[i]:
                lines[i] = _short_line(b).strip() or lines[i]
        durations = fit()
    spoken = [l for l in lines if l]
    return {
        "lines": lines,
        "durations": [round(d, 3) for d in durations],
        "muted": muted,
        "spoken_lines": len(spoken),
        "estimated_speech_s": round(sum(estimate(l) for l in spoken), 1),
        "estimated_duration_s": round(sum(durations), 1),
        "target_s": round(target, 1),
        "over_target": sum(durations) > target + 0.05,
        "characters": sum(len(l) for l in spoken),
    }


def preview(session: dict, *, length_s: float = 45.0, narrator: Optional[tts.Narrator] = None,
            title: Optional[str] = None) -> dict:
    """Storyboard plus narration plan, with no audio and no video. Free: nothing is synthesized,
    so this is how to see the exact text (and character count) a paid voice would receive."""
    board = storyboard.build(session, length_s=length_s, title=title)
    if not board["beats"]:
        raise render.RenderError("nothing to cut: the session has no recorded events")
    n = narrator or tts.Narrator("off")
    plan = plan_narration(board, length_s, n.estimate)
    if n.engine != "off":
        for b, d in zip(board["beats"], plan["durations"]):
            b["duration"] = d
        _retime(board)
    return {
        "success": True,
        "title": board["title"],
        "duration_s": round(board["duration"], 2),
        "narration": n.describe(),
        "beats": [{"kind": b["kind"], "caption": b["caption"], "start": b["start"],
                   "duration": round(b["duration"], 2), "say": plan["lines"][i]}
                  for i, b in enumerate(board["beats"])],
        "plan": {k: v for k, v in plan.items() if k not in ("lines", "durations")},
    }


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
    plan: Optional[dict] = None
    work = Path(tempfile.mkdtemp(prefix="showreel-", dir=str(out_dir)))
    try:
        if narrator is not None and narrator.engine != "off":
            if progress:
                progress("narrating")
            plan = plan_narration(board, length_s, narrator.estimate)
            for b, d in zip(board["beats"], plan["durations"]):
                b["duration"] = d
            _retime(board)
            notes.append(f"narration plan: {sum(1 for l in plan['lines'] if l)} spoken lines, "
                         f"~{plan['estimated_speech_s']} s of speech, ~{plan['estimated_duration_s']} s video "
                         f"(target {plan['target_s']} s), {plan['characters']} characters"
                         + (f", {plan['muted']} beat(s) left silent to fit" if plan["muted"] else ""))
            clips: list[tuple[int, Path]] = []
            for i, b in enumerate(board["beats"]):
                if not plan["lines"][i]:
                    continue
                clip = narrator.say(plan["lines"][i], work / f"beat{i:02d}")
                if clip is None:
                    if narrator.engine == "off":
                        break
                    continue
                need = render.audio_seconds(clip) + NARRATION_PAD + LEAD_IN
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
                placed = [(board["beats"][i]["start"] + LEAD_IN, c) for i, c in clips]
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
    if plan:
        result["narration_plan"] = {k: v for k, v in plan.items() if k not in ("lines", "durations")}
    if narrator is not None and narrator.chars_sent:
        result["elevenlabs_characters"] = narrator.chars_sent
    base.with_suffix(".json").write_text(json.dumps({"storyboard": board, "result": result}, indent=2, default=str),
                                         encoding="utf-8")
    result["media_tag"] = "MEDIA:" + result["file_path"]
    return result
