import json
import shutil
import subprocess

import pytest

from hermes_showreel.sample import _fake_key as fake_key
from hermes_showreel import Showreel, format_result

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height",
                          "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@needs_ffmpeg
@pytest.mark.parametrize("aspect,size", [("16:9", (1920, 1080)), ("9:16", (1080, 1920))])
def test_demo_renders_mp4_captions_and_poster(app, aspect, size):
    res = app.render_demo(length=12, aspect=aspect)
    assert res["success"], res
    info = probe(res["file_path"])
    v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    assert v["codec_name"] == "h264" and (v["width"], v["height"]) == size
    dur = float(info["format"]["duration"])
    assert 9.5 <= dur <= 15.0 and dur <= 89
    srt = open(res["srt_path"], encoding="utf-8").read()
    assert "-->" in srt and fake_key() not in srt
    sidecar = open(res["file_path"].replace(".mp4", ".json"), encoding="utf-8").read()
    assert fake_key() not in sidecar
    assert res["redactions"] >= 1 and res["media_tag"].startswith("MEDIA:")
    assert "Showreel cut:" in format_result(res)


@needs_ffmpeg
@pytest.mark.skipif(not (shutil.which("espeak-ng") or shutil.which("espeak")), reason="no offline voice")
def test_demo_with_local_narration_has_audio(app):
    res = app.render_demo(length=20, aspect="1:1", narration="local")
    assert res["success"], res
    info = probe(res["file_path"])
    kinds = {s["codec_type"] for s in info["streams"]}
    assert kinds == {"video", "audio"}
    assert float(info["format"]["duration"]) <= 89.5
    assert res["narration"].startswith("local voice")


def test_missing_ffmpeg_fails_cleanly(app, monkeypatch):
    from hermes_showreel import render
    monkeypatch.setattr(render, "find_ffmpeg", lambda configured="": None)
    res = app.render_demo(length=12)
    assert res["success"] is False and "ffmpeg not found" in res["error"]


def test_app_default_narration_is_off(tmp_path, store):
    class Ctx:
        def get_config(self, key, default=None):
            return default

        def current_cron_execution(self):
            return None

    n = Showreel(Ctx(), store=store).narrator()
    assert n.requested == "off" and n.engine == "off"


def test_no_session_message(app):
    res = app.render_session("")
    assert res["success"] is False and "No recorded session" in res["error"]


def test_slash_command_parsing(app, monkeypatch):
    seen = {}
    monkeypatch.setattr(Showreel, "render_session",
                        lambda self, sid, **kw: seen.update(sid=sid, **kw) or {"success": False, "error": "x"})
    app.slash("cut 30s 9:16 My launch", session_id="abc")
    assert seen == {"sid": "abc", "length": 30.0, "aspect": "9:16", "title": "My launch"}
    assert "Usage" in app.slash("help")
    assert "Showreel capture" in app.slash("status")


def test_bubble_text_drops_markdown_but_still_scrubs():
    from hermes_showreel import render
    out = render.bubble_text("The bug in `mathutil.py` was **fixed**.")
    assert out == "The bug in mathutil.py was fixed."
    assert "sk-ant-" not in render.bubble_text("key sk-ant-" + "a" * 40)
