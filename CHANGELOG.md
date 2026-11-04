# Changelog

## 0.1.0 (unreleased)
- Session capture hooks with two-pass secret scrubbing.
- Deterministic storyboard with an 89 s hard cap and failure→fix detection.
- Pillow + ffmpeg renderer (16:9, 9:16, 1:1), burned-in captions, .srt, poster, JSON storyboard.
- Narration: off / auto / local (espeak-ng, piper) / ElevenLabs (user's key) / Hermes text_to_speech.
- Narration is planned before synthesis: spoken length is estimated per line, lines are shortened
  or beats left silent so the cut fits the requested length; `hermes showreel plan` / `/showreel plan`
  preview the exact text and character count with nothing synthesized.
- Desktop half: Showreel page (sessions, moment editor with beat toggles and caption edits,
  inline player, render history), REC chip in the status bar, player card for `showreel_render`.
- Storyboard keeps a failing command and the run that fixed it together.
- Visual polish: 1080p default (`resolution` setting), content-fitted terminal and diff windows,
  no duplicate caption bars on cards, clean captions, plurals, wrapped output in 9:16.
- Default narration is `off`; `auto` uses only free offline voices; ElevenLabs only when chosen.
- `/showreel`, `showreel_render` tool, `hermes showreel` CLI, built-in demo session.
