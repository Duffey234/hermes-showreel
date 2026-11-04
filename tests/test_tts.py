import shutil
from pathlib import Path

import pytest

from hermes_showreel import tts

SECRET = "".join(["sk_", "feedface" * 6])


def test_auto_without_key_is_captions_only():
    n = tts.Narrator("auto", secret_getter=lambda name: None)
    assert n.engine == "off" and n.describe() == "captions only"


def test_auto_with_key_uses_elevenlabs_and_never_leaks_the_key(tmp_path):
    seen = {}

    def http(url, body, headers, timeout):
        seen.update(url=url, body=body, headers=dict(headers))
        return b"ID3" + b"\x00" * 1024

    n = tts.Narrator("auto", secret_getter=lambda name: SECRET if name == tts.ELEVEN_ENV else None, http=http)
    assert n.engine == "elevenlabs"
    out = n.say("Ran `pytest`", tmp_path / "b0")
    assert out and out.read_bytes().startswith(b"ID3")
    assert seen["url"].startswith("https://api.elevenlabs.io/v1/text-to-speech/")
    assert seen["headers"]["xi-api-key"] == SECRET
    assert SECRET.encode() not in seen["body"] and b"`" not in seen["body"]
    assert n.chars_sent > 0


def test_elevenlabs_failure_falls_back_without_leaking(tmp_path, monkeypatch):
    def http(url, body, headers, timeout):
        raise tts.TTSError(f"HTTP 401 invalid key {headers['xi-api-key']}")

    monkeypatch.setattr(tts, "local_backend", lambda piper_model="": None)
    n = tts.Narrator("elevenlabs", secret_getter=lambda name: SECRET, http=http)
    assert n.say("hello", tmp_path / "b0") is None
    assert n.engine == "off"
    assert all(SECRET not in note for note in n.notes) and n.notes


def test_http_refuses_other_hosts():
    with pytest.raises(tts.TTSError):
        tts._http_post("https://evil.example/v1/x", b"{}", {"xi-api-key": "k"}, 1)


def test_missing_key_for_explicit_elevenlabs(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "local_backend", lambda piper_model="": None)
    n = tts.Narrator("elevenlabs", secret_getter=lambda name: None)
    assert n.say("hello", tmp_path / "b0") is None
    assert any("not set" in x for x in n.notes)


@pytest.mark.skipif(not (shutil.which("espeak-ng") or shutil.which("espeak")), reason="no offline voice")
def test_local_voice_writes_wav(tmp_path):
    n = tts.Narrator("local")
    assert n.engine == "local"
    out = n.say("Fixed. Tests pass now.", tmp_path / "b0")
    assert out and out.suffix == ".wav" and out.stat().st_size > 1000


def test_hermes_engine_uses_dispatch(tmp_path):
    calls = []

    def dispatch(name, args):
        calls.append((name, args))
        Path(args["output_path"]).write_bytes(b"x" * 300)
        return '{"success": true, "file_path": "%s"}' % args["output_path"]

    n = tts.Narrator("hermes", dispatch_tool=dispatch)
    out = n.say("hi", tmp_path / "b0")
    assert out.exists() and calls[0][0] == "text_to_speech"
