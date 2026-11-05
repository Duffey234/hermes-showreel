from hermes_showreel import storyboard
from hermes_showreel.capture import Recorder
from hermes_showreel.sample import record_sample


def _session(store):
    return store.get_session(record_sample(Recorder(store)))


def test_story_shape(store):
    b = storyboard.build(_session(store), length_s=45)
    kinds = [x["kind"] for x in b["beats"]]
    assert kinds[0] == "title" and kinds[1] == "ask" and kinds[-1] == "outro" and kinds[-2] == "stats"
    assert "fix" in kinds and "diff" in kinds and "terminal" in kinds
    assert kinds.index("terminal") < kinds.index("diff") < kinds.index("fix")  # failure -> edit -> fix
    assert b["stats"]["tool_calls"] == 6 and b["stats"]["files_edited"] == 1
    starts = [x["start"] for x in b["beats"]]
    assert starts == sorted(starts)


def test_every_length_respects_the_hard_cap(store):
    s = _session(store)
    for L in (5, 10, 20, 30, 45, 60, 89, 120, 600):
        b = storyboard.build(s, length_s=L)
        assert b["duration"] <= storyboard.HARD_CAP_S + 0.01
        assert b["duration"] <= max(storyboard.MIN_LEN_S, min(L, 89)) + 0.01, (L, b["duration"])


def test_deterministic(store):
    s = _session(store)
    assert storyboard.build(s, length_s=40)["beats"] == storyboard.build(s, length_s=40)["beats"]


def test_empty_session():
    b = storyboard.build({"events": []}, length_s=30)
    assert [x["kind"] for x in b["beats"]] == ["title", "stats", "outro"]


def test_failure_and_fix_are_kept_together(store):
    s = _session(store)
    for L in range(25, 90, 3):  # very short cuts may keep just the fix
        kinds = [x["kind"] for x in storyboard.build(s, length_s=L)["beats"]]
        fails = sum(1 for x in storyboard.build(s, length_s=L)["beats"]
                    if x["kind"] == "terminal" and "failed" in x["caption"])
        assert (fails > 0) == ("fix" in kinds), (L, kinds)


def test_a_trailing_render_turn_is_not_part_of_the_story():
    from hermes_showreel import storyboard as sb
    ev = lambda kind, ts, **data: {"kind": kind, "ts": ts, "tool": data.pop("tool", None), "data": data}
    events = [ev("ask", 0, text="Fix the failing test."),
              ev("tool", 1, tool="terminal", args={"command": "cd /w && python -m pytest -q"}, ok=False, output="1 failed"),
              ev("tool", 2, tool="terminal", args={"command": "cd /w && python -m pytest -q"}, ok=True, output="1 passed"),
              ev("reply", 3, text="The test passes now."),
              ev("ask", 4, text="Now call showreel_render."),
              ev("reply", 5, text="MEDIA:/home/x/renders/showreel-fix-16x9.mp4")]
    board = sb.build({"events": events}, length_s=30)
    res = [b for b in board["beats"] if b["kind"] == "result"]
    assert res and res[0]["caption"] == "The test passes now."
    assert board["stats"]["wall_seconds"] == 3
    caps = " ".join(b["caption"] for b in board["beats"])
    assert "MEDIA:" not in caps and "cd /w" not in caps and "`python -m pytest -q` failed" in caps
