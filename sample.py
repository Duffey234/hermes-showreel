"""A built-in sample session (no model needed) for `hermes showreel demo` and the tests.

It mirrors a real coding turn: ask -> read -> failing test -> patch -> passing test -> reply.
The fake credential below is assembled at runtime so no secret-shaped literal sits in the source;
it exists to show that Showreel redacts it before anything is stored or drawn.
"""

from __future__ import annotations

import json
import time
import uuid

from .capture import Recorder


def _fake_key() -> str:
    return "sk-" + "showreel" + "Demo" + "0Key" + "9xQ2" * 4


def record_sample(recorder: Recorder, session_id: str = "") -> str:
    sid = session_id or f"showreel-demo-{uuid.uuid4().hex[:8]}"
    t = time.time()
    recorder.on_session_start(session_id=sid, model="hermes-demo", platform="cli")
    recorder.pre_llm_call(session_id=sid, is_first_turn=True, model="hermes-demo", platform="cli",
                          user_message="Build a tiny snake game in Python and fix whatever breaks. "
                                       f"My key is OPENAI_API_KEY={_fake_key()} if you need it.")
    recorder.post_tool_call(session_id=sid, task_id=sid, tool_name="terminal", duration_ms=420,
                            args={"command": "ls snake/"},
                            result=json.dumps({"output": "game.py\ntest_game.py\nREADME.md", "exit_code": 0}))
    recorder.post_tool_call(session_id=sid, task_id=sid, tool_name="read_file", duration_ms=40,
                            args={"path": "snake/game.py"}, result=json.dumps({"content": "def step(snake, d): ..."}))
    recorder.post_tool_call(
        session_id=sid, task_id=sid, tool_name="terminal", duration_ms=1830,
        args={"command": "python -m pytest -q snake/"},
        result=json.dumps({"output": "..F.\nFAILED test_game.py::test_wraps_at_edge - AssertionError: "
                                     "assert (20, 5) == (0, 5)\n1 failed, 3 passed in 0.12s", "exit_code": 1}))
    recorder.post_tool_call(
        session_id=sid, task_id=sid, tool_name="patch", duration_ms=95,
        args={"path": "snake/game.py",
              "old_string": "    head = (x + dx, y + dy)",
              "new_string": "    head = ((x + dx) % WIDTH, (y + dy) % HEIGHT)  # wrap at the edges"},
        result=json.dumps({"success": True}))
    recorder.post_tool_call(
        session_id=sid, task_id=sid, tool_name="terminal", duration_ms=1610,
        args={"command": "python -m pytest -q snake/"},
        result=json.dumps({"output": "....\n4 passed in 0.10s", "exit_code": 0}))
    recorder.post_tool_call(
        session_id=sid, task_id=sid, tool_name="terminal", duration_ms=600,
        args={"command": "git commit -am 'snake: wrap at the edges'"},
        result=json.dumps({"output": "[main 3f9c2ab] snake: wrap at the edges\n 1 file changed, 1 insertion(+), "
                                     "1 deletion(-)", "exit_code": 0}))
    for i in range(4):
        recorder.post_api_request(session_id=sid, usage={"input_tokens": 5200 + 800 * i, "output_tokens": 410},
                                  api_duration=2.1, response_model="hermes-demo")
    recorder.post_llm_call(session_id=sid, assistant_response=(
        "Done. The snake now wraps around the board edges instead of crashing; I fixed the head update in "
        "game.py, all 4 tests pass, and the change is committed as 3f9c2ab."))
    recorder.on_session_end(session_id=sid, completed=True, interrupted=False)
    return sid
