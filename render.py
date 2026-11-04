"""Render a storyboard to an H.264 MP4 (plus .srt captions) with Pillow frames and system ffmpeg.

ffmpeg is NOT bundled: it is found on PATH (or the ``ffmpeg_path`` setting). Every string is
scrubbed again right before it is drawn. Nothing here touches the network.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import redact

FPS = 30
SIZES = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (1080, 1080)}
SIZES_1080 = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}
RESOLUTIONS = ("1080p", "720p")
# Beats whose card already shows the caption text in full: no second copy in the caption bar
# (the .srt still carries every caption).
NO_CAPTION_BAR = ("title", "ask", "result", "outro", "stats")
FADE_FRAMES = 7

THEMES = {
    "midnight": {"bg0": (9, 12, 24), "bg1": (20, 26, 48), "panel": (16, 21, 36), "panel2": (24, 31, 52),
                 "text": (235, 239, 247), "muted": (140, 152, 178), "accent": (255, 196, 61),
                 "accent2": (120, 170, 255), "ok": (72, 210, 140), "bad": (255, 99, 99),
                 "add": (28, 74, 48), "del": (92, 32, 40), "term": (11, 14, 22)},
    "daylight": {"bg0": (244, 241, 234), "bg1": (230, 226, 216), "panel": (255, 255, 255), "panel2": (238, 236, 230),
                 "text": (24, 26, 32), "muted": (98, 104, 118), "accent": (214, 120, 0),
                 "accent2": (40, 96, 210), "ok": (20, 150, 80), "bad": (200, 40, 40),
                 "add": (210, 244, 220), "del": (250, 220, 220), "term": (28, 30, 38)},
}

_FONT_DIRS = ["/usr/share/fonts", "/usr/local/share/fonts", str(Path.home() / ".fonts"),
              "/System/Library/Fonts", "/Library/Fonts", "C:/Windows/Fonts"]
_FONT_NAMES = {
    "sans": ["Inter-Regular.ttf", "DejaVuSans.ttf", "NotoSans-Regular.ttf", "LiberationSans-Regular.ttf",
             "Arial.ttf", "arial.ttf", "Helvetica.ttc"],
    "bold": ["Inter-Bold.ttf", "DejaVuSans-Bold.ttf", "NotoSans-Bold.ttf", "LiberationSans-Bold.ttf",
             "Arial Bold.ttf", "arialbd.ttf"],
    "mono": ["JetBrainsMono-Regular.ttf", "DejaVuSansMono.ttf", "NotoSansMono-Regular.ttf",
             "LiberationMono-Regular.ttf", "Menlo.ttc", "consola.ttf"],
}
_font_cache: dict[tuple[str, int], Any] = {}
_font_paths: dict[str, Optional[str]] = {}


class RenderError(RuntimeError):
    pass


def find_ffmpeg(configured: str = "") -> Optional[str]:
    if configured:
        p = shutil.which(configured) or (configured if Path(configured).is_file() else None)
        if p:
            return p
    return shutil.which("ffmpeg")


def _find_font_file(kind: str) -> Optional[str]:
    if kind in _font_paths:
        return _font_paths[kind]
    wanted = _FONT_NAMES[kind]
    found: Optional[str] = None
    for d in _FONT_DIRS:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for name in wanted:
                if name in files:
                    cand = os.path.join(root, name)
                    if found is None or wanted.index(name) < wanted.index(os.path.basename(found)):
                        found = cand
            if found and os.path.basename(found) == wanted[0]:
                break
    _font_paths[kind] = found
    return found


def font(kind: str, size: int):
    key = (kind, size)
    if key not in _font_cache:
        path = _find_font_file(kind)
        try:
            _font_cache[key] = ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)
        except Exception:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def S(text: Any) -> str:
    """Final scrub before anything is drawn."""
    return redact.scrub(text)[0]


def caption_text(text: Any) -> str:
    """A caption as shown on screen and in the .srt: scrubbed, no markdown ticks."""
    return re.sub(r"\s{2,}", " ", S(text).replace("`", "")).strip()


def bubble_text(text: Any) -> str:
    """Chat text as drawn in a bubble: scrubbed, markdown ticks and bold markers removed."""
    t = S(text).replace("`", "").replace("**", "").replace("__", "")
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, width: int, max_lines: int = 99) -> list[str]:
    lines: list[str] = []
    for para in (text or "").split("\n"):
        words = para.split(" ")
        cur = ""
        for w in words:
            trial = (cur + " " + w).strip()
            if draw.textlength(trial, font=fnt) <= width:
                cur = trial
                continue
            if cur:
                lines.append(cur)
            # hard-break very long words
            while draw.textlength(w, font=fnt) > width and len(w) > 1:
                cut = len(w)
                while cut > 1 and draw.textlength(w[:cut], font=fnt) > width:
                    cut -= 1
                lines.append(w[:cut])
                w = w[cut:]
            cur = w
        lines.append(cur)
        if len(lines) >= max_lines:
            break
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if lines and len(lines) == max_lines and draw.textlength(lines[-1] + " …", font=fnt) <= width:
        pass
    return lines


def clip_line(draw: ImageDraw.ImageDraw, text: str, fnt, width: int) -> str:
    if draw.textlength(text, font=fnt) <= width:
        return text
    while text and draw.textlength(text + "…", font=fnt) > width:
        text = text[:-1]
    return text + "…"


class Renderer:
    def __init__(self, aspect: str = "16:9", theme: str = "midnight", ffmpeg: str = "", resolution: str = "1080p"):
        if aspect not in SIZES:
            raise RenderError(f"aspect must be one of {', '.join(SIZES)}")
        self.aspect = aspect
        self.W, self.H = (SIZES_1080 if resolution != "720p" else SIZES)[aspect]
        self.c = THEMES.get(theme, THEMES["midnight"])
        self.ffmpeg = find_ffmpeg(ffmpeg)
        self.u = min(self.W, self.H) / 720.0  # unit scale
        self._bg = self._background()

    # ── chrome ──────────────────────────────────────────────────────────────────────────────
    def _background(self) -> Image.Image:
        W, H, c = self.W, self.H, self.c
        grad = Image.new("RGB", (1, 256))
        for y in range(256):
            t = y / 255
            grad.putpixel((0, y), tuple(int(c["bg0"][i] * (1 - t) + c["bg1"][i] * t) for i in range(3)))
        bg = grad.resize((W, H))
        glow = Image.new("L", (W, H), 0)
        gd = ImageDraw.Draw(glow)
        r = int(max(W, H) * 0.45)
        gd.ellipse((W - r, -r // 2, W + r, r + r // 2), fill=40)
        glow = glow.filter(ImageFilter.GaussianBlur(int(80 * self.u)))
        accent = Image.new("RGB", (W, H), c["accent2"])
        return Image.composite(accent, bg, glow)

    def s(self, v: float) -> int:
        return int(round(v * self.u))

    def _chrome(self, img: Image.Image, idx: int, n: int, label: str) -> None:
        d = ImageDraw.Draw(img)
        c, pad = self.c, self.s(34)
        d.ellipse((pad, pad + self.s(6), pad + self.s(14), pad + self.s(20)), fill=c["bad"])
        d.text((pad + self.s(24), pad), "SHOWREEL", font=font("bold", self.s(20)), fill=c["text"])
        f = font("sans", self.s(18))
        room = self.W - 2 * pad - self.s(24) - d.textlength("SHOWREEL", font=font("bold", self.s(20))) - self.s(30)
        right = clip_line(d, f"{idx + 1}/{n}  ·  {label}", f, room)
        d.text((self.W - pad - d.textlength(right, font=f), pad + self.s(2)), right, font=f, fill=c["muted"])

    def _caption(self, img: Image.Image, text: str, alpha: float) -> None:
        if not text or alpha <= 0:
            return
        c = self.c
        d0 = ImageDraw.Draw(img)
        f = font("bold", self.s(26 if self.aspect != "9:16" else 28))
        maxw = self.W - self.s(140)
        lines = wrap(d0, text, f, maxw, max_lines=2)
        lh = self.s(36)
        box_h = lh * len(lines) + self.s(26)
        y0 = self.H - self.s(54) - box_h
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        bw = max(d0.textlength(l, font=f) for l in lines) + self.s(44)
        x0 = (self.W - bw) / 2
        od.rounded_rectangle((x0, y0, x0 + bw, y0 + box_h), radius=self.s(16), fill=(0, 0, 0, int(170 * alpha)))
        for i, l in enumerate(lines):
            tw = od.textlength(l, font=f)
            od.text(((self.W - tw) / 2, y0 + self.s(13) + i * lh), l, font=f, fill=(255, 255, 255, int(255 * alpha)))
        img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))

    def _progress(self, img: Image.Image, frac: float) -> None:
        d = ImageDraw.Draw(img)
        h = max(3, self.s(5))
        d.rectangle((0, self.H - h, self.W, self.H), fill=self.c["panel2"])
        d.rectangle((0, self.H - h, int(self.W * max(0.0, min(1.0, frac))), self.H), fill=self.c["accent"])

    def _panel(self, d: ImageDraw.ImageDraw, box, *, fill=None, outline=None, radius=None, width=2):
        d.rounded_rectangle(box, radius=radius or self.s(18), fill=fill or self.c["panel"], outline=outline,
                            width=width)

    def content_box(self) -> tuple[int, int, int, int]:
        top = self.s(96)
        bottom = self.H - self.s(170)
        side = self.s(56)
        return side, top, self.W - side, bottom

    # ── beat painters (p = 0..1 progress inside the beat) ─────────────────────────────────
    def paint(self, beat: dict, p: float, idx: int, n: int, stats: dict) -> Image.Image:
        img = self._bg.copy()
        self._chrome(img, idx, n, beat.get("title") or beat["kind"])
        getattr(self, "_paint_" + beat["kind"], self._paint_tool)(img, beat, p, stats)
        return img

    def _paint_title(self, img, beat, p, stats):
        d = ImageDraw.Draw(img)
        c = self.c
        a = ease(p * 2.2)
        f = font("bold", self.s(58 if self.aspect != "9:16" else 54))
        lines = wrap(d, S(beat["title"]), f, self.W - self.s(160), max_lines=4)
        lh = self.s(72)
        y = self.H / 2 - lh * len(lines) / 2 - self.s(40) + (1 - a) * self.s(30)
        col = tuple(int(c["bg0"][i] + (c["text"][i] - c["bg0"][i]) * a) for i in range(3))
        for l in lines:
            tw = d.textlength(l, font=f)
            d.text(((self.W - tw) / 2, y), l, font=f, fill=col)
            y += lh
        sub = "A real Hermes session, cut automatically"
        fs = font("sans", self.s(24))
        d.text(((self.W - d.textlength(sub, font=fs)) / 2, y + self.s(18)), sub, font=fs, fill=c["muted"])
        chips = []
        if stats.get("model"):
            chips.append(S(stats["model"]))
        if stats.get("redactions"):
            chips.append(f"{plural(stats['redactions'], 'secret')} redacted")
        chips.append("rendered locally")
        self._chips(d, chips, y + self.s(72), a)

    def _chips(self, d, chips, y, a=1.0):
        f = font("bold", self.s(18))
        widths = [d.textlength(t, font=f) + self.s(30) for t in chips]
        total = sum(widths) + self.s(12) * (len(chips) - 1)
        x = (self.W - total) / 2
        for t, w in zip(chips, widths):
            col = self.c["ok"] if "redacted" in t else self.c["accent2"]
            d.rounded_rectangle((x, y, x + w, y + self.s(36)), radius=self.s(18), outline=col, width=2)
            d.text((x + self.s(15), y + self.s(7)), t, font=f, fill=col)
            x += w + self.s(12)

    def _bubble(self, img, who: str, text: str, p: float, color):
        d = ImageDraw.Draw(img)
        x0, y0, x1, y1 = self.content_box()
        f = font("sans", self.s(30))
        reveal = int(len(text) * ease(p * 1.6))
        shown = text[:reveal]
        lines = wrap(d, shown, f, x1 - x0 - self.s(80), max_lines=9)
        lh = self.s(42)
        h = max(self.s(110), lh * max(1, len(lines)) + self.s(90))
        top = y0 + max(0, (y1 - y0 - h) / 2)
        self._panel(d, (x0, top, x1, top + h), fill=self.c["panel"], outline=color, width=3)
        d.text((x0 + self.s(36), top + self.s(22)), who, font=font("bold", self.s(22)), fill=color)
        y = top + self.s(62)
        for l in lines:
            d.text((x0 + self.s(36), y), l, font=f, fill=self.c["text"])
            y += lh
        if p < 0.75 and reveal < len(text):
            d.rectangle((x0 + self.s(36) + d.textlength(lines[-1] if lines else "", font=f) + 4, y - lh + 4,
                         x0 + self.s(36) + d.textlength(lines[-1] if lines else "", font=f) + 4 + self.s(12),
                         y - lh + self.s(36)), fill=color)

    def _paint_ask(self, img, beat, p, stats):
        self._bubble(img, "YOU", bubble_text(beat["body"].get("text") or beat["caption"]), p, self.c["accent"])

    def _paint_result(self, img, beat, p, stats):
        self._bubble(img, "HERMES", bubble_text(beat["body"].get("text") or beat["caption"]), p, self.c["accent2"])

    def _window(self, d, title: str, accent=None, inner_h: Optional[float] = None):
        x0, y0, x1, y1 = self.content_box()
        if inner_h is not None:  # fit the window to its content and centre it in the frame
            h = min(y1 - y0, max((y1 - y0) * 0.5, inner_h + self.s(44)))
            y0 = y0 + ((y1 - y0) - h) / 2
            y1 = y0 + h
        c = self.c
        self._panel(d, (x0, y0, x1, y1), fill=c["term"], outline=accent, width=3 if accent else 2)
        bar = self.s(44)
        d.rounded_rectangle((x0, y0, x1, y0 + bar), radius=self.s(18), fill=c["panel2"])
        d.rectangle((x0, y0 + bar - self.s(18), x1, y0 + bar), fill=c["panel2"])
        for i, col in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
            cx = x0 + self.s(26) + i * self.s(22)
            d.ellipse((cx - self.s(7), y0 + bar / 2 - self.s(7), cx + self.s(7), y0 + bar / 2 + self.s(7)), fill=col)
        f = font("sans", self.s(18))
        d.text((x0 + self.s(100), y0 + self.s(11)), clip_line(d, title, f, x1 - x0 - self.s(130)), font=f,
               fill=c["muted"])
        return x0, y0 + bar, x1, y1

    def _paint_terminal(self, img, beat, p, stats, fixed: bool = False):
        d = ImageDraw.Draw(img)
        c = self.c
        body = beat["body"]
        ok = bool(body.get("ok", True))
        accent = c["ok"] if fixed else (None if ok else c["bad"])
        fm = font("mono", self.s(22 if self.aspect != "9:16" else 20))
        lh = self.s(30)
        cx0, cy0, cx1, cy1 = self.content_box()
        full_cmd = wrap(d, "$ " + S(body.get("command") or ""), fm, cx1 - cx0 - self.s(48), max_lines=3)
        raw_out = [l for l in S(body.get("output") or "").replace("\t", "    ").splitlines() if l.strip()]
        if self.aspect == "9:16":
            n_out = sum(len(wrap(d, l, fm, cx1 - cx0 - self.s(48), max_lines=3) or [l]) for l in raw_out) or 1
        else:
            n_out = len(raw_out) or 1
        x0, y0, x1, y1 = self._window(d, "terminal", accent,
                                      inner_h=self.s(18) + lh * (len(full_cmd) + n_out) + self.s(84))
        cmd = S(body.get("command") or "")
        typed = cmd[: int(len(cmd) * ease(p * 3.0))]
        lines_avail = int((y1 - y0 - self.s(40)) / lh)
        y = y0 + self.s(18)
        cmd_lines = wrap(d, "$ " + typed, fm, x1 - x0 - self.s(48), max_lines=3)
        for l in cmd_lines:
            d.text((x0 + self.s(24), y), l, font=fm, fill=c["accent"])
            y += lh
        if p > 0.33:
            out = S(body.get("output") or "").replace("\t", "    ").splitlines()
            out = [l for l in out if l.strip()] or ["(no output)"]
            if self.aspect == "9:16":  # narrow frame: wrap long lines instead of clipping them
                out = [w for l in out for w in (wrap(d, l, fm, x1 - x0 - self.s(48), max_lines=3) or [l])]
            k = max(1, int(len(out) * ease((p - 0.33) / 0.4)))
            room = max(1, lines_avail - len(cmd_lines) - 1)
            visible = out[:k][-room:]
            for l in visible:
                col = c["text"]
                low = l.lower()
                if any(w in low for w in ("error", "failed", "traceback", "assert")):
                    col = c["bad"]
                elif any(w in low for w in ("passed", " ok", "success", "✓", "done")):
                    col = c["ok"]
                d.text((x0 + self.s(24), y), clip_line(d, l, fm, x1 - x0 - self.s(48)), font=fm, fill=col)
                y += lh
        if p > 0.72:
            code = body.get("exit_code")
            tag = ("✓ passed" if fixed else "✓ exit 0") if ok else (f"✗ exit {code}" if code is not None else "✗ failed")
            col = c["ok"] if ok else c["bad"]
            fb = font("bold", self.s(22))
            tw = d.textlength(tag, font=fb) + self.s(32)
            d.rounded_rectangle((x1 - tw - self.s(20), y1 - self.s(58), x1 - self.s(20), y1 - self.s(18)),
                                radius=self.s(20), fill=col)
            d.text((x1 - tw - self.s(4), y1 - self.s(52)), tag, font=fb, fill=(10, 12, 18))

    def _paint_fix(self, img, beat, p, stats):
        self._paint_terminal(img, beat, p, stats, fixed=True)

    def _paint_diff(self, img, beat, p, stats):
        d = ImageDraw.Draw(img)
        c = self.c
        body = beat["body"]
        path = S(body.get("path") or "file")
        fm = font("mono", self.s(21 if self.aspect != "9:16" else 19))
        lh = self.s(29)
        lines = [S(l) for l in (body.get("lines") or ["(no diff captured)"])]
        x0, y0, x1, y1 = self._window(d, path, inner_h=self.s(44) + lh * len(lines))
        room = max(1, int((y1 - y0 - self.s(30)) / lh))
        k = max(1, int(len(lines) * ease(p * 1.8)))
        visible = lines[:k][-room:]
        y = y0 + self.s(14)
        for l in visible:
            if l.startswith("+") and not l.startswith("+++"):
                d.rectangle((x0 + 3, y - 2, x1 - 3, y + lh - 4), fill=c["add"])
                col = c["ok"]
            elif l.startswith("-") and not l.startswith("---"):
                d.rectangle((x0 + 3, y - 2, x1 - 3, y + lh - 4), fill=c["del"])
                col = c["bad"]
            elif l.startswith("@@"):
                col = c["accent2"]
            else:
                col = c["text"]
            d.text((x0 + self.s(20), y), clip_line(d, l.replace("\t", "    "), fm, x1 - x0 - self.s(40)), font=fm,
                   fill=col)
            y += lh

    _img_cache: dict = {}

    def _paint_image(self, img, beat, p, stats):
        d = ImageDraw.Draw(img)
        x0, y0, x1, y1 = self._window(d, S(beat["body"].get("tool") or "image").replace("_", " "))
        path = beat["body"].get("path") or ""
        key = (path, x1 - x0, y1 - y0)
        src = self._img_cache.get(key)
        if src is None:
            try:
                with Image.open(path) as im:
                    src = im.convert("RGB")
                    box_w, box_h = x1 - x0 - self.s(24), y1 - y0 - self.s(24)
                    scale = min(box_w / src.width, box_h / src.height) * 1.08
                    src = src.resize((max(1, int(src.width * scale)), max(1, int(src.height * scale))))
            except Exception:
                src = Image.new("RGB", (x1 - x0 - self.s(24), y1 - y0 - self.s(24)), self.c["panel2"])
            self._img_cache[key] = src
        box_w, box_h = x1 - x0 - self.s(24), y1 - y0 - self.s(24)
        z = 1.0 + 0.06 * p  # slow push-in
        w, h = int(src.width / 1.08 * z), int(src.height / 1.08 * z)
        frame = src.resize((max(1, w), max(1, h)), Image.BILINEAR)
        cx, cy = frame.width // 2, frame.height // 2
        cw, ch = min(box_w, frame.width), min(box_h, frame.height)
        crop = frame.crop((cx - cw // 2, cy - ch // 2, cx - cw // 2 + cw, cy - ch // 2 + ch))
        img.paste(crop, (int(x0 + (x1 - x0 - cw) / 2), int(y0 + (y1 - y0 - ch) / 2)))

    def _paint_tool(self, img, beat, p, stats):
        d = ImageDraw.Draw(img)
        c = self.c
        body = beat["body"]
        x0, y0, x1, y1 = self.content_box()
        a = ease(p * 2.5)
        h = self.s(260)
        top = y0 + (y1 - y0 - h) / 2 + (1 - a) * self.s(24)
        self._panel(d, (x0, top, x1, top + h), outline=c["accent2"], width=2)
        d.text((x0 + self.s(36), top + self.s(28)), "TOOL", font=font("bold", self.s(18)), fill=c["muted"])
        d.text((x0 + self.s(36), top + self.s(56)), S(body.get("tool") or beat["title"]).replace("_", " "),
               font=font("bold", self.s(40)), fill=c["text"])
        detail = S(body.get("detail") or "")
        fm = font("mono", self.s(20))
        if detail:
            d.text((x0 + self.s(36), top + self.s(118)), clip_line(d, detail, fm, x1 - x0 - self.s(72)), font=fm,
                   fill=c["accent"])
        out = [l for l in S(body.get("output") or "").splitlines() if l.strip()][:3]
        y = top + self.s(160)
        for l in out:
            d.text((x0 + self.s(36), y), clip_line(d, l, fm, x1 - x0 - self.s(72)), font=fm, fill=c["muted"])
            y += self.s(28)

    def _paint_stats(self, img, beat, p, stats):
        d = ImageDraw.Draw(img)
        c = self.c
        st = beat["body"].get("stats") or stats
        tiles = [("tool calls", st.get("tool_calls", 0)), ("files edited", st.get("files_edited", 0)),
                 ("commands", st.get("commands", 0))]
        tok = int(st.get("input_tokens", 0)) + int(st.get("output_tokens", 0))
        if tok:
            tiles.append(("tokens", tok))
        elif st.get("wall_seconds"):
            tiles.append(("seconds", int(st["wall_seconds"])))
        if st.get("redactions"):
            tiles.append(("secrets redacted", st["redactions"]))
        tiles = tiles[:4]
        x0, y0, x1, y1 = self.content_box()
        cols = 2 if (self.aspect == "9:16" or len(tiles) == 4) else len(tiles)
        rows = math.ceil(len(tiles) / cols)
        gap = self.s(22)
        tw = (x1 - x0 - gap * (cols - 1)) / cols
        th = min(self.s(190), (y1 - y0 - gap * (rows - 1)) / rows)
        top = y0 + (y1 - y0 - (th * rows + gap * (rows - 1))) / 2
        k = ease(p * 1.8)
        for i, (label, val) in enumerate(tiles):
            r, col = divmod(i, cols)
            bx, by = x0 + col * (tw + gap), top + r * (th + gap)
            self._panel(d, (bx, by, bx + tw, by + th), outline=c["panel2"])
            v = int(round(val * k))
            txt = f"{v/1000:.1f}k" if label == "tokens" and val >= 1000 else str(v)
            fb = font("bold", self.s(64))
            d.text((bx + (tw - d.textlength(txt, font=fb)) / 2, by + th * 0.16), txt, font=fb,
                   fill=c["ok"] if "redacted" in label else c["accent"])
            fl = font("sans", self.s(22))
            d.text((bx + (tw - d.textlength(label, font=fl)) / 2, by + th * 0.66), label, font=fl, fill=c["muted"])

    def _paint_outro(self, img, beat, p, stats):
        d = ImageDraw.Draw(img)
        c = self.c
        a = ease(p * 2.0)
        fb = font("bold", self.s(76))
        t = "Showreel"
        y = self.H / 2 - self.s(120)
        d.text(((self.W - d.textlength(t, font=fb)) / 2, y + (1 - a) * self.s(20)), t, font=fb, fill=c["accent"])
        fs = font("sans", self.s(26))
        sub = "Every Hermes session is a demo waiting to happen."
        for i, l in enumerate(wrap(d, sub, fs, self.W - self.s(140), 2)):
            d.text(((self.W - d.textlength(l, font=fs)) / 2, y + self.s(108) + i * self.s(36)), l, font=fs,
                   fill=c["text"])
        fm = font("mono", self.s(22))
        cmd = "hermes plugins install showreel"
        w = d.textlength(cmd, font=fm) + self.s(40)
        bx = (self.W - w) / 2
        by = y + self.s(200)
        d.rounded_rectangle((bx, by, bx + w, by + self.s(50)), radius=self.s(12), fill=c["term"])
        d.text((bx + self.s(20), by + self.s(12)), cmd, font=fm, fill=c["ok"])
        fl = font("sans", self.s(18))
        foot = "rendered locally · nothing uploaded · secrets redacted before drawing"
        d.text(((self.W - d.textlength(foot, font=fl)) / 2, by + self.s(78)), foot, font=fl, fill=c["muted"])

    # ── encode ──────────────────────────────────────────────────────────────────────────────
    def render(self, board: dict, out_path: Path, *, audio_path: Optional[Path] = None,
               progress: Optional[Callable[[float], None]] = None, deadline_s: float = 600.0) -> dict:
        if not self.ffmpeg:
            raise RenderError("ffmpeg not found on PATH. Install it (apt install ffmpeg / brew install ffmpeg / "
                              "winget install ffmpeg) or set plugins.entries.showreel.settings.ffmpeg_path.")
        beats = board["beats"]
        total = float(board["duration"])
        stats = board.get("stats") or {}
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_video = out_path.with_suffix(".video.mp4") if audio_path else out_path
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{self.W}x{self.H}", "-r", str(FPS), "-i", "-", "-an", "-c:v", "libx264", "-preset",
               "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(tmp_video)]
        started = time.monotonic()
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        frames = 0
        prev_last: Optional[Image.Image] = None
        try:
            for i, b in enumerate(beats):
                nf = max(1, int(round(b["duration"] * FPS)))
                last = None
                for f in range(nf):
                    p = f / max(1, nf - 1)
                    img = self.paint(b, p, i, len(beats), stats)
                    if prev_last is not None and f < FADE_FRAMES:
                        img = Image.blend(prev_last, img, (f + 1) / (FADE_FRAMES + 1))
                    t_global = (b["start"] + b["duration"] * p) / max(total, 0.001)
                    if b["kind"] not in NO_CAPTION_BAR:
                        self._caption(img, caption_text(b["caption"]), ease(min(1.0, f / 8)))
                    self._progress(img, t_global)
                    proc.stdin.write(img.tobytes())
                    frames += 1
                    last = img
                    if time.monotonic() - started > deadline_s:
                        raise RenderError(f"render exceeded {deadline_s:.0f}s and was stopped")
                prev_last = last
                if progress:
                    progress((i + 1) / len(beats))
            proc.stdin.close()
            err = proc.stderr.read().decode(errors="replace")
            if proc.wait() != 0:
                raise RenderError("ffmpeg failed: " + S(err)[-400:])
        except BrokenPipeError:
            err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            proc.wait()
            raise RenderError("ffmpeg stopped early: " + S(err)[-400:])
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        if audio_path:
            mux = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(tmp_video), "-i", str(audio_path),
                   "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(out_path)]
            r = subprocess.run(mux, capture_output=True, timeout=180)
            if r.returncode != 0:
                raise RenderError("ffmpeg audio mux failed: " + S(r.stderr.decode(errors='replace'))[-400:])
            tmp_video.unlink(missing_ok=True)
        return {"frames": frames, "seconds": round(frames / FPS, 2), "render_seconds": round(time.monotonic() - started, 1),
                "size": [self.W, self.H]}

    def poster(self, board: dict, out_path: Path, beat_index: int = 0) -> Path:
        beats = board["beats"]
        b = beats[min(beat_index, len(beats) - 1)]
        img = self.paint(b, 1.0, beat_index, len(beats), board.get("stats") or {})
        if b["kind"] not in NO_CAPTION_BAR:
            self._caption(img, caption_text(b["caption"]), 1.0)
        img.save(out_path)
        return out_path


def write_srt(board: dict, path: Path) -> Path:
    def ts(sec: float) -> str:
        ms = int(round(sec * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    out = []
    for i, b in enumerate(board["beats"], 1):
        out.append(f"{i}\n{ts(b['start'])} --> {ts(b['start'] + b['duration'])}\n{caption_text(b['caption'])}\n")
    Path(path).write_text("\n".join(out), encoding="utf-8")
    return Path(path)


def mix_narration(clips: list[tuple[float, Path]], total_s: float, out_path: Path, ffmpeg: str,
                  rate: int = 24000) -> Path:
    """Place each clip (start_seconds, file) on a silent mono track of *total_s* seconds."""
    n = int(total_s * rate)
    buf = bytearray(n * 2)
    tmpdir = Path(out_path).parent
    for k, (start, src) in enumerate(clips):
        norm = tmpdir / f".narr_{k}.wav"
        r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-ac", "1", "-ar",
                            str(rate), "-sample_fmt", "s16", str(norm)], capture_output=True, timeout=60)
        if r.returncode != 0 or not norm.exists():
            continue
        with wave.open(str(norm), "rb") as w:
            data = w.readframes(w.getnframes())
        norm.unlink(missing_ok=True)
        off = int(start * rate) * 2
        end = min(len(buf), off + len(data))
        if off < end:
            buf[off:end] = data[: end - off]
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(buf))
    return Path(out_path)


def audio_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        pass
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=30)
        try:
            return float(r.stdout.strip())
        except ValueError:
            return 0.0
    return 0.0
