"""Scrubbing: remove secrets and personal paths from anything Showreel stores or draws.

Runs twice: when a hook records an event (so the plugin database never holds a raw secret)
and again on every string right before it is drawn into a frame, caption or subtitle file.
Pure standard library; no network, no file reads.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Tuple

MASK = "[redacted]"

# (label, compiled pattern). Order matters: specific vendor shapes first, generic last.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key_block", re.compile(
        r"-----BEG" r"IN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}")),
    ("anthropic_or_openai_key", re.compile(r"\bsk-(?:ant-|proj-|or-)?[A-Za-z0-9_\-]{20,}")),
    ("elevenlabs_key", re.compile(r"\bsk_[a-f0-9]{40,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("hf_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("bearer", re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9_\-\.=+/]{16,}")),
    ("url_credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{0,20}://)[^\s/:@]{1,128}:[^\s/@]{1,256}@")),
    ("hex_key_64", re.compile(r"(?<![0-9a-fA-Fx])(?:0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])")),
]

# key=value / "key": "value" shapes whose key names a secret.
_ASSIGN = re.compile(
    r"""(?ix)
    (?P<key>["']?[A-Za-z0-9_.\-]{0,40}(?:api[_-]?key|secret|token|passw(?:or)?d|passwd|private[_-]?key|
        mnemonic|seed[_-]?phrase|auth|credential|cookie|session[_-]?id)[A-Za-z0-9_.\-]{0,40}["']?)
    (?P<sep>\s*(?:=|:)\s*)
    (?P<q>["']?)
    (?P<val>[^\s"',;}{]{6,512})
    (?P=q)
    """)

_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|key|token|access_token|auth|sig|signature|secret|password)=)[^&\s#]+")

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){0,6}\.[A-Za-z]{2,24}\b")

_CANDIDATE = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")


def _entropy(s: str) -> float:
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _looks_random(tok: str) -> bool:
    """Opaque high-entropy token with mixed case and digits (API-key shaped, not a hex sha)."""
    if re.fullmatch(r"[0-9a-fA-F]+", tok):  # git shas, digests: shown on purpose
        return False
    if "/" in tok and tok.count("/") >= 2:  # looks like a path
        return False
    has_l = any(c.islower() for c in tok)
    has_u = any(c.isupper() for c in tok)
    has_d = any(c.isdigit() for c in tok)
    return has_l and has_u and has_d and _entropy(tok) >= 4.2


def _home_variants() -> list[str]:
    homes = set()
    try:
        homes.add(os.path.expanduser("~"))
    except Exception:  # pragma: no cover
        pass
    return sorted((h for h in homes if h and h not in ("/", "~") and len(h) > 2), key=len, reverse=True)


def scrub(text: Any, *, emails: bool = True, paths: bool = True) -> Tuple[str, int]:
    """Return ``(clean_text, number_of_redactions)``. Never raises; non-strings are str()-ed."""
    if text is None:
        return "", 0
    if not isinstance(text, str):
        text = str(text)
    count = 0
    for _label, pat in _PATTERNS:
        if _label == "url_credentials":
            text, n = pat.subn(lambda m: m.group(1) + MASK + "@", text)
        elif _label == "bearer":
            text, n = pat.subn(lambda m: m.group(1) + " " + MASK, text)
        else:
            text, n = pat.subn(MASK, text)
        count += n

    def _assign(m: re.Match[str]) -> str:
        nonlocal count
        val = m.group("val")
        if MASK in val or val.lower() in ("true", "false", "none", "null", "required", "optional"):
            return m.group(0)
        if val.startswith("$"):  # env reference like ${API_KEY}, not a value
            return m.group(0)
        count += 1
        return f"{m.group('key')}{m.group('sep')}{m.group('q')}{MASK}{m.group('q')}"

    text = _ASSIGN.sub(_assign, text)
    text, n = _QUERY_SECRET.subn(lambda m: m.group(1) + MASK, text)
    count += n

    def _cand(m: re.Match[str]) -> str:
        nonlocal count
        tok = m.group(0)
        if MASK in tok or not _looks_random(tok):
            return tok
        count += 1
        return MASK

    text = _CANDIDATE.sub(_cand, text)
    if emails:
        text, n = _EMAIL.subn("[email]", text)
        count += n
    if paths:
        for home in _home_variants():
            text = text.replace(home, "~")
    return text, count


def scrub_obj(obj: Any, *, emails: bool = True) -> Tuple[Any, int]:
    """Scrub every string inside dicts/lists (keys that name secrets get their values masked)."""
    total = 0
    if isinstance(obj, str):
        return scrub(obj, emails=emails)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and is_secret_key(k) and isinstance(v, (str, int, float)) and str(v):
                out[k] = MASK
                total += 1
                continue
            out[k], n = scrub_obj(v, emails=emails)
            total += n
        return out, total
    if isinstance(obj, (list, tuple)):
        items = []
        for v in obj:
            c, n = scrub_obj(v, emails=emails)
            items.append(c)
            total += n
        return items, total
    return obj, 0


_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|secret|token|passw|private[_-]?key|mnemonic|seed|credential|cookie|authorization)")


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(key)) and not key.lower().endswith(("_count", "_tokens", "tokens"))
