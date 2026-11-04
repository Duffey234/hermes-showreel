import json

from hermes_showreel.sample import _fake_key as fake_key
from hermes_showreel.capture import Recorder, Store
from hermes_showreel.sample import record_sample


def test_sample_session_is_recorded_and_scrubbed(tmp_path):
    db = tmp_path / "data.db"
    store = Store(db)
    sid = record_sample(Recorder(store))
    s = store.get_session(sid)
    kinds = [e["kind"] for e in s["events"]]
    assert kinds.count("tool") == 6 and kinds.count("ask") == 1 and kinds.count("reply") == 1
    assert kinds.count("api") == 4
    assert s["redactions"] >= 1 and s["completed"] == 1
    assert s["first_prompt"].startswith("Build a tiny snake game")
    store.close()
    raw = db.read_bytes()
    for f in db.parent.glob("data.db*"):
        raw += f.read_bytes()
    assert fake_key().encode() not in raw, "a secret reached the database"


def test_tool_results_parse_status(store):
    rec = Recorder(store)
    rec.post_tool_call(session_id="s1", tool_name="terminal", args={"command": "false"},
                       result=json.dumps({"output": "", "exit_code": 1}), duration_ms=5)
    rec.post_tool_call(session_id="s1", tool_name="web_search", args={"query": "x"},
                       result=json.dumps({"error": "rate limited"}), duration_ms=5)
    rec.post_tool_call(session_id="s1", tool_name="terminal", args={"command": "true"}, result="not json",
                       duration_ms=5)
    ev = store.get_session("s1")["events"]
    assert [e["data"]["ok"] for e in ev] == [False, False, True]
    assert ev[0]["data"]["exit_code"] == 1


def test_hooks_never_raise_and_ignore_garbage(store):
    rec = Recorder(store)
    rec.post_tool_call()  # no ids
    rec.post_tool_call(session_id="s", tool_name="terminal", args="not a dict", result=object())
    rec.pre_llm_call(session_id="s", user_message=None)
    rec.post_api_request(session_id="s", usage="nope")
    rec.on_session_end(session_id="s")
    assert rec.pre_llm_call(session_id="s", user_message="hi") is None  # observer: never injects


def test_own_tool_calls_are_not_recorded(store):
    Recorder(store).post_tool_call(session_id="s", tool_name="showreel_render", args={}, result="{}")
    assert store.get_session("s") is None


def test_capture_toggle(store):
    on = {"v": False}
    rec = Recorder(store, enabled=lambda: on["v"])
    rec.pre_llm_call(session_id="s", user_message="hello")
    assert store.get_session("s") is None
    on["v"] = True
    rec.pre_llm_call(session_id="s", user_message="hello")
    assert len(store.get_session("s")["events"]) == 1


def test_large_output_is_truncated(store):
    Recorder(store).post_tool_call(session_id="s", tool_name="terminal", args={"command": "yes"},
                                   result=json.dumps({"output": "y\n" * 50000, "exit_code": 0}))
    out = store.get_session("s")["events"][0]["data"]["output"]
    assert len(out) < 7000 and "more chars" in out


def test_prune_and_purge(store):
    rec = Recorder(store)
    for i in range(5):
        rec.pre_llm_call(session_id=f"s{i}", user_message=f"task {i}")
    assert store.prune(2) == 3 and len(store.list_sessions()) == 2
    assert store.purge("s4") == 1
    assert store.purge() == 1 and store.list_sessions() == []


def test_images_are_referenced_not_copied(store, tmp_path):
    from PIL import Image
    p = tmp_path / "shot.png"
    Image.new("RGB", (40, 30), (200, 10, 10)).save(p)
    Recorder(store).post_tool_call(session_id="s", tool_name="browser_screenshot", args={},
                                   result=json.dumps({"success": True, "media_tag": f"MEDIA:{p}"}))
    assert store.get_session("s")["events"][0]["data"]["images"] == [str(p)]
