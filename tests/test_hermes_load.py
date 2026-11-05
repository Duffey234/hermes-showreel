"""Load the plugin through Hermes's REAL discovery path in a scratch HERMES_HOME and drive a real
tool call through model_tools, so post_tool_call fires from core. Skipped outside a Hermes venv."""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

try:
    import hermes_cli  # noqa: F401
    HAVE_HERMES = True
except Exception:
    HAVE_HERMES = False

SCRIPT = textwrap.dedent(r"""
    import json, sys
    from hermes_cli.plugins import discover_plugins, get_plugin_manager, invoke_hook, get_plugin_commands
    import model_tools
    discover_plugins(force=True)
    pm = get_plugin_manager()
    info = [p for p in pm.list_plugins() if p.get("name") == "showreel"]
    sid = "itest-session-1"
    invoke_hook("on_session_start", session_id=sid, model="m", platform="cli")
    invoke_hook("pre_llm_call", session_id=sid, user_message="read the notes file", conversation_history=[],
                is_first_turn=True, model="m", platform="cli")
    res = model_tools.handle_function_call("read_file", {"path": sys.argv[1]}, task_id=sid, session_id=sid)
    invoke_hook("post_llm_call", session_id=sid, user_message="x", assistant_response="Read it.",
                conversation_history=[], model="m", platform="cli")
    invoke_hook("on_session_end", session_id=sid, completed=True, interrupted=False, model="m", platform="cli")
    from tools.registry import registry
    print("RESULT" + json.dumps({
        "plugin": info, "tool_registered": "showreel_render" in registry.get_all_tool_names(),
        "commands": sorted(get_plugin_commands().keys()), "read_ok": "error" not in res[:200].lower()}))
""")


@pytest.mark.skipif(not HAVE_HERMES, reason="Hermes not importable")
def test_real_discovery_and_core_hooks(tmp_path):
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(ROOT, home / "plugins" / "showreel",
                    ignore=shutil.ignore_patterns("tests", ".git", "__pycache__", ".pytest_cache"))
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - showreel\n", encoding="utf-8")
    notes = tmp_path / "notes.txt"
    notes.write_text("API_KEY=" + "sk-" + "itest" * 6 + "\nhello\n", encoding="utf-8")
    env = dict(os.environ, HERMES_HOME=str(home))
    r = subprocess.run([sys.executable, "-c", SCRIPT, str(notes)], capture_output=True, text=True, env=env,
                       timeout=240, cwd=str(tmp_path))
    line = [l for l in r.stdout.splitlines() if l.startswith("RESULT")]
    assert line, r.stdout[-2000:] + r.stderr[-3000:]
    out = json.loads(line[0][6:])
    assert out["tool_registered"] and "showreel" in out["commands"], out
    db = home / "plugin-data" / "showreel" / "data.db"
    assert db.exists()
    import sqlite3
    con = sqlite3.connect(db)
    rows = con.execute("SELECT kind, tool, data FROM events WHERE session_id='itest-session-1' ORDER BY id").fetchall()
    con.close()
    kinds = [r[0] for r in rows]
    assert kinds == ["ask", "tool", "reply"], rows
    assert rows[1][1] == "read_file"
    blob = b"".join(p.read_bytes() for p in db.parent.glob("data.db*"))
    assert ("sk-" + "itest" * 6).encode() not in blob
