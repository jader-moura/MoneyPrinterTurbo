# Timeline editor (Phase A1)

CapCut-style timeline for reviewed tasks. Additive: the one-click path and the
Phase 1 review list both keep working untouched.

## Why not OpenCut, Twick or designcombo

| Candidate | License | Blocker |
| --- | --- | --- |
| OpenCut | MIT | A standalone app, not a library. React 19 + TanStack Start over a Rust/WASM core, and it **exports in the browser**. There is no `<Timeline/>` to import. |
| Twick | Sustainable Use License | Not OSI open source, and it bundles `posthog-js` telemetry into a local-first app. |
| designcombo `react-video-editor` | OpenVideo | Two-tier; a company license is required above a size threshold. |
| `@xzdarcy/react-timeline-editor` | MIT | A bare timeline widget: no video preview, no trim semantics. |

All four render client-side. This project renders server-side in
`app/services/video.py`, which owns subtitles, transitions, concat modes, fit
modes and voice/BGM mixing. Adopting any of their export paths would discard
that work — exactly what the "add, never remove" rule forbids.

So: **their UI idea, our renderer.** The timeline describes an edit; the
existing Python pipeline renders it.

## The fact that shapes the design

`combine_videos()` (video.py:976) derives every clip length from the narration
audio duration plus `max_clip_duration`. It slices each source into fixed
chunks, drops the surplus, and loops clips when the audio outruns the footage.
There is no per-clip in/out anywhere.

But `process_one_clip()` already works on a `SubClippedVideoClip` carrying
explicit `start_time`/`end_time`. So an explicit edit list slots in exactly
where the automatic slicing happens, and *everything downstream is reused*:
fit mode, speed, transitions, encoding, concat, subtitles.

Second fact: **generated images are pre-rendered to MP4 at a fixed duration**
(`material._render_openai_image_video` → `video.render_image_zoom_video`), so
an image clip's source is only as long as it was baked. The source PNG does
survive in the task directory, and `render_image_zoom_video` is content-addressed
by `(image bytes, duration)`, so extending an image clip is a re-render from
that PNG — not a stretch of the baked MP4.

## Steps

1. **`app/services/timeline.py`** (new) — the edit-list model. Probe source
   durations, resolve an image clip back to its source PNG, normalise
   `trim_start`/`duration` against the real source length, and build the edit
   list the renderer consumes. Re-renders an image at a longer duration when
   asked to extend it.
2. **`video.py`: `combine_videos(..., edit_list=None)`** — additive. When given,
   build `subclipped_items` from it and skip the automatic slicing, the
   unique-source prioritisation, the duration fill loop, the clip cycling and
   the `max_clip_duration` truncation. When `None`, behaviour is unchanged.
3. **`scene_manifest.py`** — read `trim_start` / `duration` with defaults
   (`0.0` / `None`). Not written by `build_scenes`, so old manifests and the
   existing tests are unaffected; they appear only once a user sets them.
4. **`scene_editor.py`** — `update_timing()`, and `prepare_render()` emits the
   edit list.
5. **`task.py`** — thread the edit list into the one `combine_videos` call.
6. **`controllers/v1/editor.py`** — `PUT /scenes/{i}/timing`, and the timeline
   payload (source durations, bounds, stream URLs).
7. **`resource/public/editor/`** — the SPA, served at `/editor/` by the existing
   `StaticFiles` mount on `resource/public`.
8. A link into it from the Phase 1 review panel.

## Why vanilla JS and no build step

This is a Python repo. A React/Vite toolchain would add `npm install`, a build
directory, a CI step and a Node requirement for everyone who clones it, to ship
one page. The timeline is pointer-event drag maths plus a `<video>` element, so
it is written as one dependency-free page: `index.html` + `editor.js` +
`editor.css`, served by the mount that already exists. No build, no CDN, works
offline.

## As built

Where each piece landed, and the three places reality differed from the plan.

| Step | Landed in |
| --- | --- |
| 1 | `app/services/timeline.py` (new, ~300 lines) |
| 2 | `video.py`: `edit_list` param + `_subclips_from_edit_list()` |
| 3 | `scene_manifest.py`: `scene_timing`, `has_timing_edits`, `patch_scene_timing`, `clear_scene_timing`, `edit_list_for_render` |
| 4 | `scene_editor.py`: `update_timing`, `_point_scene_at`, `prepare_render` writes the edit list |
| 5 | `task.py`: `generate_final_videos(..., edit_list=None)`, read on resume |
| 6 | `controllers/v1/editor.py`: `GET /timeline`, `PUT /scenes/{i}/timing` |
| 7 | `resource/public/editor/{index.html,editor.js,editor.css}` |
| 8 | `webui/review.py`: `_render_timeline()` embeds it + 3 i18n keys × 14 locales |
| 9 | `scripts/start-local.sh`: starts the API server before the WebUI |

**Deviation 4 — the editor is embedded, not just linked.** A link alone meant
leaving the WebUI, and the obvious alternative (popping a tab open from
Streamlit) is unreliable because a `window.open` without a user gesture is
blocked. Streamlit's component iframe carries
`allow-same-origin allow-scripts allow-popups`, so the editor keeps its own
origin inside the frame: its `fetch`, `<video>` and `localStorage` all behave
normally and no CORS is involved. The panel probes `/docs` (cached 15s) and
explains how to start the API when it is not answering.

**Deviation 5 — two bugs only a running server exposed.**
`_require_task` consulted `state` alone, but `state` is per-process while
`enable_redis` is off, so every editor route returned 404 for a task created in
the WebUI; it now falls back to the manifest, which is this feature's source of
truth anyway. And `/stream` hardcoded `media_type="video/mp4"`, so narration
MP3s were mislabelled and would not decode in the preview's `<audio>`; it now
guesses from the filename and falls back to `video/mp4`, leaving video
behaviour unchanged.

**Deviation 1 — `_normalise_scene` was a whitelist.** It rebuilds every scene
from a fixed set of keys, on both save *and* load, so `trim_start`/`duration`
could never persist. It now carries them through, but only when present, so an
untouched scene serialises to exactly the same keys as before.

**Deviation 2 — the edit list stores basenames.** `relative_media_path()`
returns `<task_id>/<file>` for `/stream` URLs, but `resolve_material()`
resolves against the task directory, so that form does not round-trip. The
stored edit list uses the basename, matching how `scenes[].material` works.

**Deviation 3 — a regression of my own, in `webui_task`.** Threading
`stop_at`/`start_from` through `_enqueue_generation` positionally pushed
`codex_image_context` off the end of the positional arguments, which
`test_codex_image_task.py` reads as `args[-1]`. They are now passed as
keywords. The test is unchanged.

The progress denominator also had to change: with an edit list the clip loop
reports against the timeline total, not the narration duration, or the bar
stalls partway or runs past 100%.

## Known limits of A1

* No overlay/text track — that needs real compositing, not concatenation.
  Subtitles stay with the existing burn-in path.
* Scene timings drive the picture only. Narration audio is not re-timed, so
  laying out clips shorter than the narration leaves the voice running past the
  end; the editor warns when the layout total drifts from the narration length.
* Trim is in source time. With `video_clip_speed != 1.0` the on-screen length is
  `(end - start) / speed`; the editor shows the effective length.
* Replacing a scene with your own upload stays in the review panel. The API
  route takes a filename the upload endpoint already returned, so the timeline
  would need its own uploader to offer it.
* **Opening the timeline probes every material.** `GET /timeline` opens each
  clip to read its true length, because the UI cannot draw trim bounds without
  it. That is one decoder start per scene on the first load, then cached by
  `(path, mtime, size)`. `GET /scenes` deliberately does not probe.
* **Extending an image scans the task directory.** The MP4 baked from an image
  is named by `sha256(image bytes + path + duration)`, and nothing records which
  PNG it came from, so finding the source means recomputing that digest for each
  image in the directory. It runs only when a scene is actually held longer, and
  if no match is found the duration is clamped and the reason logged rather than
  failing the render.
