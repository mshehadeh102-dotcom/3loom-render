# Free-first production upgrades — علوم بلهجتنا

This is an implementation roadmap, not a claim that external software is installed.

## What has been implemented on this branch

- Subtitle timeline boundaries stay within narration duration; regression tests added.
- A standard-library preflight validator runs before GitHub Actions render.
- It blocks malformed media URLs, unexpected hosts, excessive clip count, oversized encoded audio, invalid loudness and accidental publishing.
- Production/main, n8n, Telegram and the Qannas server are untouched.

## Upstream research (reviewed, not copied)

- PurffleShorts (MIT): https://github.com/Chamanrajragu/purffle-shorts
  - Its studio HTTP API supports queued jobs, polling and video retrieval. Useful later if deployed to an isolated worker; not required for the current renderer.
  - Worth adapting: word-level caption alignment, scene-aware clip choices, ffmpeg xfade, script quality scoring.
- OpenMontage (AGPL-3.0): https://github.com/calesthio/OpenMontage
  - Worth adapting as *ideas*, not copy-pasting: storyboard review, render QA gates, free footage selection, cost decision log.
  - AGPL terms require review before incorporating its code into a deployed service.
- Manim Community (MIT): https://github.com/ManimCommunity/manim
  - Generate explanatory diagrams as MP4 on a separate worker, then feed them into the existing clip list. No video-generation API required.
- FFmpeg: existing renderer; keep it as the default final encoder.

## Next integration order

1. Add scene duration and shot purpose fields to the n8n job schema, then make the renderer honor them. Preserve compatibility with old jobs.
2. Add optional SRT/word timestamps from a speech aligner; do not fabricate word-level sync from character counts.
3. Run automated render QA (ffprobe, blank frame detection, audio stream, caption safe area, scene continuity checks).
4. Test Manim CPU-based science diagrams and compare with actual stock footage.
5. Evaluate PurffleShorts in an isolated service only after measuring RAM/CPU usage. Never colocate it with the Qannas bot without resource isolation.
6. Evaluate ComfyUI/Wan only if free scene sources cannot deliver acceptable cinematic shots; require an explicit spending ceiling and GPU auto-shutdown.

## Publishing and spending

No automatic publishing; Telegram approval remains mandatory. No GPU purchase, rental or paid API calls are authorized by this branch. GitHub Actions usage and public-release privacy must be reviewed before live jobs.
