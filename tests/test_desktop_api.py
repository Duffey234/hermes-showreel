"""The Desktop half's backend routes (dashboard/plugin_api.py) and the desktop/plugin.js surface."""

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")


@pytest.fixture
def client(app, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    fake = types.ModuleType("hermes_plugins.showreel")
    fake.__file__ = str(ROOT / "__init__.py")
    fake._APP = app
    monkeypatch.setitem(sys.modules, "hermes_plugins.showreel", fake)
    spec = importlib.util.spec_from_file_location("showreel_plugin_api_under_test", ROOT / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    api = FastAPI()
    api.include_router(mod.router, prefix="/api/plugins/showreel")
    return TestClient(api)


def test_status_sessions_plan_render_media(client, app):
    s = client.get("/api/plugins/showreel/status").json()
    assert s["capture"] is True and s["narration"] == "off" and "recording" in s
    from hermes_showreel.capture import Recorder
    from hermes_showreel.sample import record_sample
    sid = record_sample(Recorder(app.store))
    assert client.get("/api/plugins/showreel/status").json()["recording"] is True  # events just now
    rows = client.get("/api/plugins/showreel/sessions").json()["sessions"]
    assert rows and rows[0]["session_id"] == sid
    plan = client.get(f"/api/plugins/showreel/plan?session_id={sid}&length=20").json()
    assert plan["success"] and plan["beats"] and plan["narration"].startswith("captions only")
    if not __import__("shutil").which("ffmpeg"):
        return
    res = client.post("/api/plugins/showreel/render", json={"session_id": sid, "length": 12, "aspect": "1:1",
                                                            "edits": {"skip": [2]}}).json()
    assert res["success"], res
    names = [r["name"] for r in client.get("/api/plugins/showreel/renders").json()["renders"]]
    assert res["name"] in names
    media = client.get(f"/api/plugins/showreel/media?name={res['name']}").json()
    assert media["url"].startswith("data:video/mp4;base64,")


def test_media_refuses_paths_outside_renders(client):
    for bad in ("../data.db", "/etc/passwd", "showreel-x/../../data.db", ".env", "data.db", "showreel-a.py"):
        assert client.get("/api/plugins/showreel/media", params={"name": bad}).status_code == 404


def test_render_refuses_unknown_engine(client):
    assert client.post("/api/plugins/showreel/render", json={"narration": "rm -rf"}).status_code == 400


def test_desktop_half_imports_only_the_sdk_and_react():
    src = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
    specs = set(re.findall(r"""from\s+['"]([^'"]+)['"]""", src))
    assert specs <= {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}
    assert "eval(" not in src and "new Function" not in src and "import(" not in src
    assert "hermesDesktop" not in src and "fetch(" not in src  # backend only through ctx.rest
