# Changelog

## 0.1.0 (unreleased)
- Session capture hooks with two-pass secret scrubbing.
- Deterministic storyboard with an 89 s hard cap and failure→fix detection.
- Pillow + ffmpeg renderer (16:9, 9:16, 1:1), burned-in captions, .srt, poster, JSON storyboard.
- Narration: off / auto / local (espeak-ng, piper) / ElevenLabs (user's key) / Hermes text_to_speech.
- `/showreel`, `showreel_render` tool, `hermes showreel` CLI, built-in demo session.
