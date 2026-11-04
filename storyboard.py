"""Turn a recorded session into an ordered list of beats that fits a hard time budget.

Deterministic: the same session and settings always give the same storyboard. Captions come
from templates; an optional LLM pass (``captions.polish``) may rewrite them later.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Optional

HARD_CAP_S = 89.0     # never longer than this (the hackathon / social limit is 90 s)
MIN_LEN_S = 10.0

# Base seconds per beat kind (stretched or squeezed to fit).
BASE = {"title": 3.2, "ask": 4.5, "terminal": 5.0, "diff": 5.5, "image": 4.5, "fix": 5.5,
        "tool": 3.6, "result": 5.5, "stats": 3.8, "outro": 3.0}
MIN_BEAT = {"title": 2.2, "ask": 3.0, "terminal": 3.0, "diff": 3.2, "image": 3.0, "fix": 3.5,
            "tool": 2.4, "result": 3.2, "stats": 2.6, "outro": 2.0}

TERMINAL_TOOLS = {"terminal", "shell", "bash", "execute_code", "run_command", "process"}
EDIT_TOOLS = {"write_file", "patch", "edit_file", "file_edit", "apply_patch", "str_replace", "create_file"}
IMAGE_TOOLS = {"browser_screenshot", "image_generate", "browser_vision", "vision_analyze", "computer_use"}
QUIET_TOOLS = {"todo", "memory", "clarify", "session_search", "skill_view", "skills_list", "read_file",
               "search_files", "list_files", "tool_search", "tool_describe"}


@dataclass
class Beat:
    kind: str
    title: str
    caption: str
    body: dict = field(default_factory=dict)
    score: float = 0.0
    order: int = 0
    duration: float = 0.0
    start: float = 0.0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "title": self.title, "caption": self.caption, "score": round(self.score, 2),
                "duration": round(self.duration, 2), "start": round(self.start, 2), "body": self.body}


def _short(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip(" .,;:-") + "…"


def _headline(text: str, n: int = 60) -> str:
    """First sentence of the ask, without trailing punctuation."""
    first = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text or "").strip(), maxsplit=1)[0]
    return _short(first, n).rstrip(".!")


def _cmd_of(args: dict) -> str:
    for k in ("command", "cmd", "code", "script"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _path_of(args: dict) -> str:
    for k in ("path", "file_path", "filename", "file", "target"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _diff_lines(args: dict) -> list[str]:
    """Best-effort +/- lines for an edit tool call."""
    for k in ("patch", "diff"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return [l for l in v.splitlines() if l.strip()][:40]
    old = args.get("old_string") or args.get("old_str") or args.get("old") or args.get("search")
    new = args.get("new_string") or args.get("new_str") or args.get("new") or args.get("replace")
    if isinstance(old, str) or isinstance(new, str):
        lines = ["- " + l for l in (old or "").splitlines()][:15]
        lines += ["+ " + l for l in (new or "").splitlines()][:20]
        return lines
    content = args.get("content") or args.get("text")
    if isinstance(content, str):
        return ["+ " + l for l in content.splitlines()][:30]
    return []


def _first_word(cmd: str) -> str:
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()
    return parts[0] if parts else ""


_TEST_RE = re.compile(r"\b(pytest|npm (?:run )?test|jest|vitest|go test|cargo test|unittest|make test|node --test)\b")


def _is_test(cmd: str) -> bool:
    return bool(_TEST_RE.search(cmd))


def build(session: dict, *, length_s: float = 45.0, title: Optional[str] = None) -> dict:
    """Return ``{"beats": [...], "duration": s, "stats": {...}}`` for *session* (from ``Store.get_session``)."""
    target = max(MIN_LEN_S, min(float(length_s or 45.0), HARD_CAP_S))
    events = session.get("events") or []
    asks = [e for e in events if e["kind"] == "ask"]
    replies = [e for e in events if e["kind"] == "reply"]
    tools = [e for e in events if e["kind"] == "tool"]
    apis = [e for e in events if e["kind"] == "api"]

    t0 = events[0]["ts"] if events else (session.get("started") or 0)
    t1 = events[-1]["ts"] if events else t0
    stats = {
        "tool_calls": len(tools),
        "failed_calls": sum(1 for e in tools if not e["data"].get("ok", True)),
        "files_edited": len({_path_of(e["data"].get("args") or {}) for e in tools if (e["tool"] or "") in EDIT_TOOLS} - {""}),
        "commands": sum(1 for e in tools if (e["tool"] or "") in TERMINAL_TOOLS),
        "input_tokens": sum(int(e["data"].get("input_tokens") or 0) for e in apis),
        "output_tokens": sum(int(e["data"].get("output_tokens") or 0) for e in apis),
        "wall_seconds": max(0.0, float(t1 - t0)),
        "redactions": int(session.get("redactions") or 0),
        "model": session.get("model") or "",
    }

    ask_text = asks[0]["data"].get("text", "") if asks else (session.get("first_prompt") or "")
    headline = title or (_headline(ask_text) if ask_text else "A Hermes session")

    fixed_head = [Beat("title", headline, headline,
                       {"subtitle": "made with Showreel", "model": stats["model"]}, 100)]
    if ask_text:
        fixed_head.append(Beat("ask", "You asked", _short(ask_text, 110), {"text": _short(ask_text, 420)}, 99))

    # Score tool beats.
    middle: list[Beat] = []
    seen_tools: set[str] = set()
    last_fail_idx: dict[str, int] = {}
    for i, e in enumerate(tools):
        name = e["tool"] or "tool"
        d = e["data"]
        args = d.get("args") or {}
        ok = bool(d.get("ok", True))
        out = d.get("output") or ""
        score = 1.0
        if name in QUIET_TOOLS:
            score = 0.2
        novelty = 1.5 if name not in seen_tools else 0.0
        seen_tools.add(name)
        if name in TERMINAL_TOOLS:
            cmd = _cmd_of(args)
            key = "test" if _is_test(cmd) else _first_word(cmd)
            kind = "terminal"
            score = 3.0 + (1.5 if _is_test(cmd) else 0)
            if not ok:
                score += 1.0
                last_fail_idx[key] = len(middle)
                cap = f"`{_short(cmd, 48)}` failed"
            elif key in last_fail_idx:
                score += 4.0
                kind = "fix"
                cap = f"Fixed. `{_short(cmd, 40)}` passes now"
                prev = middle[last_fail_idx.pop(key)]
                prev.score += 2.5  # the failure is part of the story
            else:
                cap = f"Ran `{_short(cmd, 52)}`"
            body = {"command": cmd, "output": out, "ok": ok, "exit_code": d.get("exit_code")}
            middle.append(Beat(kind, "Terminal" if kind != "fix" else "Fixed", cap, body, score + novelty, i))
        elif name in EDIT_TOOLS:
            path = _path_of(args)
            lines = _diff_lines(args)
            adds = sum(1 for l in lines if l.startswith("+"))
            dels = sum(1 for l in lines if l.startswith("-"))
            cap = f"Edited {os.path.basename(path) or 'a file'}  (+{adds} / -{dels})"
            middle.append(Beat("diff", "Edit", cap, {"path": path, "lines": lines, "ok": ok}, 4.0 + novelty, i))
        elif d.get("images"):
            img = d["images"][0]
            cap = {"browser_screenshot": "Checked it in the browser", "image_generate": "Generated an image"}.get(
                name, f"{name.replace('_', ' ').capitalize()}")
            middle.append(Beat("image", "Look", cap, {"path": img, "tool": name}, 5.0 + novelty, i))
        else:
            detail = _short(_cmd_of(args) or _path_of(args) or str(args.get("query") or args.get("url") or ""), 50)
            cap = f"{name.replace('_', ' ')}" + (f": {detail}" if detail else "")
            middle.append(Beat("tool", name.replace("_", " "), cap,
                               {"tool": name, "detail": detail, "output": out, "ok": ok}, score + novelty, i))
    # The last successful terminal beat is usually the payoff.
    for b in reversed(middle):
        if b.kind in ("terminal", "fix") and b.body.get("ok"):
            b.score += 1.0
            break

    reply = replies[-1]["data"].get("text", "") if replies else ""
    tail: list[Beat] = []
    if reply:
        tail.append(Beat("result", "Result", _short(reply, 110), {"text": _short(reply, 600)}, 98))
    tail.append(Beat("stats", "By the numbers", _stats_caption(stats), {"stats": stats}, 97))
    tail.append(Beat("outro", "Showreel", "Cut by Showreel for Hermes Agent", {}, 96))

    beats = _fit(fixed_head, middle, tail, target)
    t = 0.0
    for b in beats:
        b.start = t
        t += b.duration
    return {"beats": [b.to_dict() for b in beats], "duration": round(t, 2), "target": target, "stats": stats,
            "title": headline}


def _stats_caption(s: dict) -> str:
    parts = [f"{s['tool_calls']} tool calls"]
    if s["files_edited"]:
        parts.append(f"{s['files_edited']} files edited")
    mins = s["wall_seconds"] / 60.0
    if mins >= 1:
        parts.append(f"{mins:.0f} min")
    elif s["wall_seconds"] >= 1:
        parts.append(f"{s['wall_seconds']:.0f} s")
    tok = s["input_tokens"] + s["output_tokens"]
    if tok:
        parts.append(f"{tok/1000:.1f}k tokens" if tok >= 1000 else f"{tok} tokens")
    return " · ".join(parts)


def _fit(head: list[Beat], middle: list[Beat], tail: list[Beat], target: float) -> list[Beat]:
    """Pick the best middle beats, keep chronological order, and scale durations into *target*."""
    fixed = head + tail
    fixed_min = sum(MIN_BEAT[b.kind] for b in fixed)
    budget_mid = max(0.0, target - sum(BASE[b.kind] for b in fixed))
    chosen: list[Beat] = []
    used = 0.0
    for b in sorted(middle, key=lambda b: (-b.score, b.order)):
        if b.score < 0.5:
            continue
        cost = BASE[b.kind]
        if used + cost <= budget_mid + 0.01:
            chosen.append(b)
            used += cost
    # If nothing fits at base length, keep the single best beat at its minimum.
    if not chosen and middle and target - fixed_min >= MIN_BEAT["terminal"]:
        best = max(middle, key=lambda b: b.score)
        if best.score >= 0.5:
            chosen = [best]
    chosen.sort(key=lambda b: b.order)
    beats = head + chosen + tail
    for b in beats:
        b.duration = BASE[b.kind]
    total = sum(b.duration for b in beats)
    if total > target:  # squeeze toward minimums
        _scale(beats, target)
    elif total < target and chosen:  # stretch content beats a little, never past 1.6x
        extra = target - total
        weight = sum(BASE[b.kind] for b in chosen) or 1.0
        for b in chosen:
            b.duration = min(BASE[b.kind] * 1.6, b.duration + extra * BASE[b.kind] / weight)
    total = sum(b.duration for b in beats)
    if total > HARD_CAP_S:
        _scale(beats, HARD_CAP_S)
    return beats


def _scale(beats: list[Beat], target: float) -> None:
    total = sum(b.duration for b in beats)
    floor = sum(MIN_BEAT[b.kind] for b in beats)
    if total <= target:
        return
    if floor >= target:  # drop lowest-score middle beats until it fits
        while len(beats) > 3 and sum(MIN_BEAT[b.kind] for b in beats) > target:
            mids = [b for b in beats if b.kind not in ("title", "stats", "outro")]
            if not mids:
                break
            beats.remove(min(mids, key=lambda b: b.score))
        for b in beats:
            b.duration = MIN_BEAT[b.kind]
        return
    ratio = (target - floor) / (total - floor)
    for b in beats:
        b.duration = MIN_BEAT[b.kind] + (b.duration - MIN_BEAT[b.kind]) * ratio
