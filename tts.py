"""Narration engines. Pluggable; the default is ``off`` (captions only).

* ``off``        – the default: captions only, no audio.
* ``auto``       – a free offline voice when one is installed, else captions only. Never a paid service.
* ``local``      – free offline voice: ``piper`` (with a configured voice model) or ``espeak-ng`` /
                   ``espeak`` found on PATH. Nothing is downloaded or bundled.
* ``elevenlabs`` – only when explicitly chosen: the user's own ElevenLabs key (``ELEVENLABS_API_KEY``),
                   read with ``agent.secret_scope.get_secret``.
                   One HTTPS POST per narrated beat to api.elevenlabs.io (bills the user's account).
* ``hermes``     – Hermes's own ``text_to_speech`` tool with whatever TTS the user configured.

A key is never logged, printed, stored, or written into any file Showreel produces.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from . import redact

ENGINES = ("off", "auto", "local", "elevenlabs", "hermes")
ELEVEN_HOST = "https://api.elevenlabs.io"
ELEVEN_ENV = "ELEVENLABS_API_KEY"
DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"   # a stock ElevenLabs voice id ("Rachel")
DEFAULT_MODEL = "eleven_turbo_v2_5"
MAX_CHARS = 400


class TTSError(RuntimeError):
    pass


# Speaking rates used to plan narration before any audio exists (words per minute).
WPM = {"local": 165.0, "elevenlabs": 150.0, "hermes": 155.0, "off": 160.0}


def estimate_seconds(text: str, wpm: float = 160.0) -> float:
    """Rough spoken length of *text*: words at *wpm*, plus a short pause per sentence or clause
    break, plus extra time for long tokens (numbers, paths) that are read out letter by letter."""
    words = re.findall(r"[A-Za-z0-9']+", text or "")
    if not words:
        return 0.0
    units = sum(1.0 + max(0, len(w) - 9) / 6.0 for w in words)
    pauses = len(re.findall(r"[.!?]+(?:\s|$)", text)) * 0.30 + len(re.findall(r"[,;:]\s", text)) * 0.15
    return round(units * 60.0 / max(60.0, wpm) + pauses, 2)


def _get_secret(name: str) -> Optional[str]:
    try:
        from agent.secret_scope import get_secret  # documented credential route
    except Exception:  # running outside Hermes (unit tests inject a getter)
        return None
    try:
        return get_secret(name)
    except Exception:
        return None


def local_backend(piper_model: str = "") -> Optional[tuple[str, str]]:
    """(kind, executable) of the first offline voice found, or None."""
    if piper_model and Path(piper_model).expanduser().is_file():
        exe = shutil.which("piper")
        if exe:
            return "piper", exe
    for name in ("espeak-ng", "espeak"):
        exe = shutil.which(name)
        if exe:
            return "espeak", exe
    return None


class Narrator:
    def __init__(self, engine: str = "off", *, voice: str = "", model: str = "", local_voice: str = "en-us",
                 piper_model: str = "", secret_getter: Callable[[str], Optional[str]] = _get_secret,
                 http: Optional[Callable[..., bytes]] = None, dispatch_tool: Optional[Callable[..., str]] = None,
                 fallback_local: bool = True):
        if engine not in ENGINES:
            raise TTSError(f"narration must be one of {', '.join(ENGINES)}")
        self.requested = engine
        self.voice = voice or DEFAULT_VOICE
        self.model = model or DEFAULT_MODEL
        self.local_voice = local_voice or "en-us"
        self.piper_model = piper_model
        self._secret = secret_getter
        self._http = http or _http_post
        self._dispatch = dispatch_tool
        self.fallback_local = fallback_local
        self.engine = self._resolve(engine)
        self.notes: list[str] = []
        self.chars_sent = 0

    def _resolve(self, engine: str) -> str:
        if engine == "auto":  # free voices only; a paid service is never picked implicitly
            return "local" if local_backend(self.piper_model) else "off"
        if engine == "local" and not local_backend(self.piper_model):
            return "off"
        return engine

    def _has_key(self) -> bool:
        try:
            return bool(self._secret(ELEVEN_ENV))
        except Exception:
            return False

    def describe(self) -> str:
        if self.engine == "off":
            if self.requested == "local":
                return "captions only (no offline voice found: install espeak-ng or piper)"
            return "captions only"
        if self.engine == "local":
            kind, _exe = local_backend(self.piper_model) or ("?", "")
            return f"local voice ({kind})"
        if self.engine == "elevenlabs":
            return "ElevenLabs (your key)"
        return "Hermes text_to_speech"

    @property
    def wpm(self) -> float:
        return WPM.get(self.engine, 160.0)

    def estimate(self, text: str) -> float:
        return estimate_seconds(text, self.wpm)

    # ── synthesis ──────────────────────────────────────────────────────────────────────────
    def say(self, text: str, out_base: Path) -> Optional[Path]:
        """Synthesize *text*; return the audio path, or None when narration is off/failed."""
        text = redact.scrub(text)[0].replace("`", "").strip()[:MAX_CHARS]
        if not text or self.engine == "off":
            return None
        try:
            if self.engine == "elevenlabs":
                return self._eleven(text, out_base.with_suffix(".mp3"))
            if self.engine == "hermes":
                return self._hermes(text, out_base.with_suffix(".mp3"))
            return self._local(text, out_base.with_suffix(".wav"))
        except Exception as exc:
            self.notes.append(f"{self.engine} narration failed ({redact.scrub(str(exc))[0][:160]})")
            if self.engine != "local" and self.fallback_local and local_backend(self.piper_model):
                self.notes.append("switched to the local voice")
                self.engine = "local"
                try:
                    return self._local(text, out_base.with_suffix(".wav"))
                except Exception as exc2:
                    self.notes.append(f"local narration failed ({redact.scrub(str(exc2))[0][:160]})")
            self.engine = "off"
            return None

    def _local(self, text: str, out: Path) -> Path:
        backend = local_backend(self.piper_model)
        if not backend:
            raise TTSError("no offline voice on PATH")
        kind, exe = backend
        if kind == "piper":
            r = subprocess.run([exe, "--model", str(Path(self.piper_model).expanduser()), "--output_file", str(out)],
                               input=text.encode(), capture_output=True, timeout=120)
        else:
            r = subprocess.run([exe, "-v", self.local_voice, "-s", "165", "-w", str(out), "--", text],
                               capture_output=True, timeout=60)
        if r.returncode != 0 or not out.exists():
            raise TTSError(f"{kind} exited {r.returncode}")
        return out

    def _eleven(self, text: str, out: Path) -> Path:
        key = self._secret(ELEVEN_ENV)
        if not key:
            raise TTSError(f"{ELEVEN_ENV} is not set")
        url = f"{ELEVEN_HOST}/v1/text-to-speech/{self.voice}?output_format=mp3_44100_128"
        body = json.dumps({"text": text, "model_id": self.model}).encode()
        audio = self._http(url, body, {"xi-api-key": key, "Content-Type": "application/json",
                                       "Accept": "audio/mpeg"}, 60)
        del key
        if not audio or len(audio) < 256:
            raise TTSError("empty audio from ElevenLabs")
        out.write_bytes(audio)
        self.chars_sent += len(text)
        return out

    def _hermes(self, text: str, out: Path) -> Path:
        if self._dispatch is None:
            raise TTSError("Hermes tool dispatch unavailable here")
        raw = self._dispatch("text_to_speech", {"text": text, "output_path": str(out)})
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        path = data.get("file_path") if isinstance(data, dict) else None
        if not path or not Path(path).exists():
            raise TTSError(str((data or {}).get("error") or "text_to_speech returned no file"))
        return Path(path)


def _http_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> bytes:
    if not url.startswith(ELEVEN_HOST + "/"):
        raise TTSError("refusing to send the key to a non-ElevenLabs host")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https host)
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="replace")[:200]
        except Exception:
            pass
        raise TTSError(f"ElevenLabs HTTP {e.code} {redact.scrub(detail)[0]}") from None
    except urllib.error.URLError as e:
        raise TTSError(f"ElevenLabs unreachable: {type(e.reason).__name__}") from None
