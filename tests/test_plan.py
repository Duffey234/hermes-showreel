"""Narration is planned (spoken length estimated) before any audio is generated."""

from hermes_showreel import cut, storyboard, tts
from hermes_showreel.capture import Recorder
from hermes_showreel.sample import record_sample


def _board(store, length):
    return storyboard.build(store.get_session(record_sample(Recorder(store))), length_s=length)


def test_estimate_seconds_scales_with_words_and_rate():
    assert tts.estimate_seconds("") == 0.0
    short, long_ = tts.estimate_seconds("Tests pass."), tts.estimate_seconds("Tests pass. " * 10)
    assert 0.5 < short < 2.0 and long_ > 8 * short
    assert tts.estimate_seconds("one two three four", 120) > tts.estimate_seconds("one two three four", 200)


def test_plan_fits_the_requested_length(store):
    for length in (12, 15, 20, 30, 45, 60, 89):
        board = _board(store, length)
        plan = cut.plan_narration(board, length, lambda t: tts.estimate_seconds(t, 150))
        if length >= 30:
            assert plan["estimated_duration_s"] <= length + 0.1 and not plan["over_target"], length
        else:  # very short cuts may run a little long to keep the story audible, and say so
            assert plan["estimated_duration_s"] <= length * 1.3, length
            assert plan["over_target"] == (plan["estimated_duration_s"] > length + 0.05)
        assert len(plan["lines"]) == len(plan["durations"]) == len(board["beats"])
        for b, line, d in zip(board["beats"], plan["lines"], plan["durations"]):
            if b["kind"] in ("ask", "result"):
                assert line, (length, b["kind"])  # always spoken (the title is folded into the ask)
            if line:
                assert d + 1e-6 >= tts.estimate_seconds(line, 150) + cut.LEAD_IN + cut.NARRATION_PAD
            assert "`" not in (line or "")
        assert plan["characters"] == sum(len(l) for l in plan["lines"] if l)


def test_tight_length_mutes_middle_beats_not_the_story(store):
    board = _board(store, 12)
    plan = cut.plan_narration(board, 12, lambda t: tts.estimate_seconds(t, 120))
    kinds = {b["kind"] for b in board["beats"]}
    spoken = {b["kind"] for b, l in zip(board["beats"], plan["lines"]) if l}
    assert {"ask", "result"} & kinds <= spoken
    assert plan["muted"] >= 1


def test_only_planned_lines_are_synthesized(store, tmp_path):
    said = []

    class Recording(tts.Narrator):
        def say(self, text, out_base):
            said.append(text)
            return None  # no audio: captions stay, the cut still renders

    n = Recording("off")
    n.engine = "local"  # pretend a voice exists; nothing is actually spoken
    session = store.get_session(record_sample(Recorder(store)))
    res = cut.cut(session, tmp_path, length_s=12, narrator=n)
    plan = res["narration_plan"]
    assert len(said) == plan["spoken_lines"] < len(res["beats"])
    assert sum(len(t) for t in said) == plan["characters"]


def test_preview_synthesizes_nothing(store):
    calls = []
    n = tts.Narrator("elevenlabs", secret_getter=lambda name: "x" * 40,
                     http=lambda *a, **k: calls.append(1) or b"")
    session = store.get_session(record_sample(Recorder(store)))
    res = cut.preview(session, length_s=30, narrator=n)
    assert res["success"] and res["plan"]["characters"] > 0 and calls == [] and n.chars_sent == 0
    assert abs(sum(b["duration"] for b in res["beats"]) - res["plan"]["estimated_duration_s"]) < 0.2


def test_moment_edits_drop_beats_and_replace_captions(store):
    session = store.get_session(record_sample(Recorder(store)))
    board = storyboard.build(session, length_s=45)
    kinds = [b["kind"] for b in board["beats"]]
    i_tool, i_title = kinds.index("tool"), kinds.index("title")
    i_ask = kinds.index("ask")
    edited = cut.apply_edits(board, {"skip": [i_tool, i_title], "captions": {str(i_ask): "Make it wrap. key=sk-" + "A" * 30}}, 45)
    new_kinds = [b["kind"] for b in edited["beats"]]
    assert "title" in new_kinds and new_kinds.count("tool") == kinds.count("tool") - 1  # title cannot be dropped
    ask = next(b for b in edited["beats"] if b["kind"] == "ask")
    assert ask["caption"].startswith("Make it wrap.") and "A" * 30 not in ask["caption"]  # edited text is scrubbed
    assert abs(edited["duration"] - 45) < 0.5 or all(b["duration"] >= cut.MAX_BEAT_S - 0.01 for b in edited["beats"])
    same = storyboard.build(session, length_s=45)
    assert cut.apply_edits(same, {"skip": [], "captions": {}}, 45)["duration"] == storyboard.build(session, length_s=45)["duration"]
